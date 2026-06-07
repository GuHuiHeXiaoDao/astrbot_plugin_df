# -*- coding: utf-8 -*-
"""
AstrBot 插件：DF Helper / 矮人要塞攻略查询助手

特性：
- /df 关键词：模糊匹配本地词条。
- /df_help：显示帮助。
- 命中后阻断事件继续进入 LLM。
- 命中词条后优先发送 OneBot 合并转发：文字 + 多图。
- 图片按 df_entries.json 中 images 顺序发送。
- video_url / video_urls 在聊天记录外额外发送。
- 合并转发节点使用机器人真实 QQ 昵称与头像。
- 词条数据优先保存在 StarTools.get_data_dir() 指向的数据目录，避免插件升级覆盖数据。
- 支持 entries/ 多文件词条库，便于多人协作：一个词条一个 JSON 文件。
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import time
import traceback
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api.all import EventMessageType
except ImportError:
    try:
        from astrbot.api.event.filter import EventMessageType
    except ImportError:
        EventMessageType = None

try:
    from astrbot.api.star import StarTools
except ImportError:
    StarTools = None


PLUGIN_NAME = "astrbot_plugin_df_helper"
PLUGIN_VERSION = "1.3.9"
PLUGIN_DIR = Path(__file__).resolve().parent


def normalize_text(text: str) -> str:
    """归一化用于模糊匹配的文本。"""
    if text is None:
        return ""

    normalized = unicodedata.normalize("NFKC", str(text)).lower().strip()
    return re.sub(
        r"[\s\-_·・,，.。:：;；!！?？()\[\]【】{}<>《》\"'“”‘’/\\|]+",
        "",
        normalized,
    )


def stop_event_safely(event: AstrMessageEvent) -> None:
    """
    命中 /df 或 /df_help 后，尽量阻止该消息继续进入后续处理链/LLM。

    AstrBot 不同版本的停止传播 API 可能不同，所以这里做多路兼容。
    """
    for method_name in (
        "stop_event",
        "stop_propagation",
        "prevent_default",
        "block",
        "stop",
    ):
        method = getattr(event, method_name, None)
        if callable(method):
            try:
                method()
                return
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"DF Helper: {method_name} failed: {exc}")

    for attr_name, value in (
        ("is_stopped", True),
        ("stopped", True),
        ("is_blocked", True),
        ("continue_event", False),
        ("should_call_llm", False),
        ("enable_llm", False),
    ):
        try:
            setattr(event, attr_name, value)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"DF Helper: set {attr_name} failed: {exc}")


def strip_leading_mentions(line: str) -> str:
    """清理行首 @ 与 CQ at。"""
    cleaned = line.strip()
    cleaned = re.sub(
        r"^(?:\[CQ:at,[^\]]+\]\s*)+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(r"^(?:@\S+\s+)+", "", cleaned).strip()
    return cleaned


def last_effective_line(raw: str) -> str:
    """
    只取最后一行有效文本作为当前命令行。

    目的：
    - 避免引用消息里的旧 /df 再次触发；
    - 支持 @机器人 换行 /df 铁矿；
    - 避免普通聊天中出现“我刚刚用了 /df 铁矿”被误触发。
    """
    if not raw:
        return ""

    text = unicodedata.normalize("NFKC", str(raw))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\[CQ:reply,[^\]]+\]", "", text, flags=re.IGNORECASE)

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return ""

    return strip_leading_mentions(lines[-1])


def is_df_help(raw: str) -> bool:
    """
    判断最后一行是否为帮助指令。

    正式帮助指令：
    - /df help

    兼容旧写法：
    - /df_help
    - /df-help
    """
    line = last_effective_line(raw)
    patterns = (
        r"(?i)^/df\s+help(?:\s|$)",
        r"(?i)^/df[_-]help(?:\s|$)",
    )
    return any(re.match(pattern, line) is not None for pattern in patterns)

def is_df_command_line(raw: str) -> bool:
    """判断最后一行是否以 /df 开头，但排除 /df help / /df_help。"""
    if is_df_help(raw):
        return False

    line = last_effective_line(raw)
    return re.match(r"(?i)^/df(?![_-]?help\b)", line) is not None

def split_df_query(raw: str) -> str:
    """
    从 /df 指令中提取查询词。

    支持：
    - /df 铁矿
    - /df铁矿
    - @机器人 /df 铁矿
    - @机器人 换行 /df 铁矿

    注意：/df help 是帮助指令，不会被当成 query=help。
    """
    line = last_effective_line(raw)
    if not line or is_df_help(raw):
        return ""

    if not re.match(r"(?i)^/df(?![_-]?help\b)", line):
        return ""

    return re.sub(r"(?i)^/df", "", line, count=1).strip()

def get_current_message_text(event: AstrMessageEvent) -> str:
    """
    尽量读取当前用户真实输入文本，跳过 Reply / Quote / At 组件。

    如果读取消息链失败，回退到 event.get_message_str()。
    """
    try:
        message_obj = getattr(event, "message_obj", None)
        chain = None

        for attr_name in ("message", "messages", "message_chain", "chain"):
            value = getattr(message_obj, attr_name, None) if message_obj else None
            if isinstance(value, list):
                chain = value
                break

        if isinstance(chain, list):
            parts: List[str] = []
            for component in chain:
                class_name = component.__class__.__name__.lower()
                component_type = str(getattr(component, "type", "")).lower()

                if "reply" in class_name or "quote" in class_name:
                    continue
                if component_type in {"reply", "quote", "at"}:
                    continue
                if class_name == "at" or class_name.endswith(".at"):
                    continue

                text = None
                for attr_name in ("text", "content", "message"):
                    value = getattr(component, attr_name, None)
                    if isinstance(value, str) and value:
                        text = value
                        break

                if text is not None:
                    parts.append(text)

            if parts:
                return "".join(parts)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"DF Helper: read message chain failed: {exc}")

    try:
        return event.get_message_str()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"DF Helper: event.get_message_str failed: {exc}")
        return str(getattr(event, "message_str", "") or "")



def render_text(value: Any) -> str:
    """
    渲染 JSON 文本字段。

    兼容两种情况：
    - JSON 标准换行：\\n 会被 json.load 自动变成真实换行；
    - 用户手动写成两个字符 "\\n" 时，也转换成真实换行。
    """
    text = str(value or "")
    return text.replace("\\n", "\n").replace("/n", "\n")


def read_json_file(path: Path, default: Any) -> Any:
    """读取 JSON，并明确记录不同错误类型。"""
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        logger.warning(f"DF Helper: JSON file not found: {path}")
        return default
    except JSONDecodeError as exc:
        logger.error(f"DF Helper: invalid JSON in {path}: {exc}")
        return default
    except OSError as exc:
        logger.error(f"DF Helper: failed to read JSON {path}: {exc}")
        return default


def write_json_file(path: Path, data: Any) -> None:
    """写入 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def default_entries() -> Dict[str, Any]:
    """默认词条。首次启动数据目录为空时写入。"""
    return {
        "aliases": {
            "赤铁": "铁矿",
            "磁铁": "铁矿",
            "褐铁": "铁矿",
            "三铁矿": "铁矿",
            "三种铁矿石": "铁矿",
        },
        "entries": [
            {
                "id": "df_iron_ores",
                "title": "三种铁矿",
                "author": "本地整理",
                "keywords": [
                    "铁矿",
                    "赤铁矿",
                    "磁铁矿",
                    "褐铁矿",
                    "铁矿石",
                    "hematite",
                    "magnetite",
                    "limonite",
                    "iron ore",
                ],
                "tags": [
                    "矿石",
                    "金属",
                    "冶炼",
                ],
                "answer": (
                    "游戏中存在三种可冶炼为铁锭的矿石："
                    "赤铁矿(hematite)、磁铁矿(magnetite)和褐铁矿(limonite)。\n\n"
                    "如果想查看其他矿石的详细属性和挖掘后的矿石图样，可以点击左下角的锤子按钮，"
                    "选择“stone use”（石头用途），再选择“economic stone”（经济性石头），"
                    "即可查看各类石头的金属含量、用途、是否耐岩浆等特性。"
                    "其他普通石头也可以在“Other stone”中查看。"
                ),
                "images": [],
                "video_urls": [],
            }
        ],
    }


def default_help_catalog() -> Dict[str, Any]:
    """默认帮助分类目录。"""
    return {
        "title": "DF 查询分类帮助",
        "description": "每个分类会变成合并转发聊天记录中的一条节点，方便维护大分类。",
        "categories": [
            {
                "id": "minerals",
                "title": "矿物与金属",
                "summary": "矿物总览：常用金属矿、经济性石头、岩浆安全材料等。",
                "items": [
                    {
                        "title": "三种铁矿",
                        "query": "铁矿",
                        "keywords": ["赤铁矿", "磁铁矿", "褐铁矿"],
                        "desc": "可冶炼为铁锭的三类矿石。"
                    },
                    {
                        "title": "铜矿",
                        "query": "铜矿",
                        "keywords": ["孔雀石", "自然铜", "黝铜矿"],
                        "desc": "铜及铜合金相关矿石。"
                    }
                ]
            },
            {
                "id": "machines",
                "title": "机械与动力",
                "summary": "水车、齿轮、轴、螺旋泵、压力板等工程系统。",
                "items": [
                    {
                        "title": "水车",
                        "query": "水车",
                        "keywords": ["水轮", "机械动力"],
                        "desc": "机械动力来源。"
                    },
                    {
                        "title": "螺旋泵",
                        "query": "螺旋泵",
                        "keywords": ["泵", "抽水"],
                        "desc": "流体工程核心设备。"
                    }
                ]
            }
        ]
    }


def get_plugin_data_dir() -> Path:
    """
    获取 AstrBot 推荐的数据目录。

    优先 StarTools.get_data_dir()；如果当前版本不可用，则回退到插件目录 data。
    """
    if StarTools is not None:
        try:
            data_dir = StarTools.get_data_dir()
            return Path(data_dir)
        except TypeError:
            try:
                data_dir = StarTools.get_data_dir(PLUGIN_NAME)
                return Path(data_dir)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"DF Helper: StarTools.get_data_dir(name) failed: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"DF Helper: StarTools.get_data_dir failed: {exc}")

    return PLUGIN_DIR / "data"


def resolve_data_file(configured_path: str, data_dir: Path) -> Path:
    """解析 data_file，支持绝对路径；相对路径相对于数据目录。"""
    path = Path(configured_path).expanduser()
    if path.is_absolute():
        return path

    if configured_path.startswith("data/"):
        return data_dir / configured_path.removeprefix("data/")

    return data_dir / path


def ensure_initial_data(
    data_file: Path,
    entries_dir: Path,
    image_dir: Path,
    help_catalog_file: Path,
    help_dir: Path,
) -> None:
    """
    初始化数据目录，避免插件升级覆盖用户数据。

    词条：
    - df_entries.json 可选；
    - entries/**/*.json 会一次性全部加载。

    帮助：
    - help_catalog.json 可维护总分类；
    - help/**/*.json 可拆分维护分类帮助。
    """
    data_file.parent.mkdir(parents=True, exist_ok=True)
    entries_dir.mkdir(parents=True, exist_ok=True)
    image_dir.mkdir(parents=True, exist_ok=True)
    help_catalog_file.parent.mkdir(parents=True, exist_ok=True)
    help_dir.mkdir(parents=True, exist_ok=True)

    has_split_entries = any(entries_dir.rglob("*.json"))

    if not has_split_entries:
        bundled_file = PLUGIN_DIR / "data" / "df_entries.json"
        if bundled_file.exists():
            bundled_data = read_json_file(bundled_file, default={})
            entries_raw = []
            aliases_raw = {}

            if isinstance(bundled_data, dict):
                aliases_raw = bundled_data.get("aliases", {})
                entries_raw = bundled_data.get("entries", [])
            elif isinstance(bundled_data, list):
                entries_raw = bundled_data

            if aliases_raw and not data_file.exists():
                write_json_file(data_file, {"aliases": aliases_raw, "entries": []})

            if isinstance(entries_raw, list) and entries_raw:
                for index, item in enumerate(entries_raw, 1):
                    if not isinstance(item, dict):
                        continue
                    entry_id = str(item.get("id") or item.get("title") or f"entry_{index}")
                    safe_name = normalize_text(entry_id) or f"entry_{index}"
                    write_json_file(entries_dir / f"{safe_name}.json", item)
        else:
            sample_data = default_entries()
            entries = sample_data.get("entries", [])
            if entries:
                write_json_file(entries_dir / "iron_ores.json", entries[0])

    if not data_file.exists():
        write_json_file(data_file, {"aliases": {}, "entries": []})

    # 帮助目录不覆盖已有文件；只在完全没有帮助文件时创建示例。
    has_help_files = help_catalog_file.exists() or any(help_dir.rglob("*.json"))
    if not has_help_files:
        write_json_file(help_catalog_file, default_help_catalog())


@dataclass
class DFEntry:
    """DF 查询词条。"""

    id: str
    title: str
    keywords: List[str]
    answer: str
    images: List[str]
    tags: List[str]
    author: str
    video_urls: List[str]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DFEntry":
        images = data.get("images", data.get("image", []))
        if isinstance(images, str):
            images = [images]

        keywords = data.get("keywords", [])
        if isinstance(keywords, str):
            keywords = [keywords]

        tags = data.get("tags", [])
        if isinstance(tags, str):
            tags = [tags]

        video_urls = data.get("video_urls", data.get("video_url", data.get("video", [])))
        if isinstance(video_urls, str):
            video_urls = [video_urls]

        title = str(data.get("title", data.get("name", data.get("id", "")))).strip()
        entry_id = str(data.get("id", normalize_text(title))).strip() or normalize_text(title)

        return cls(
            id=entry_id,
            title=title,
            keywords=[str(item).strip() for item in keywords if str(item).strip()],
            answer=render_text(
                data.get(
                    "answer",
                    data.get(
                        "text",
                        data.get("content", data.get("description", data.get("desc", ""))),
                    ),
                )
            ).strip(),
            images=[str(item).strip() for item in images if str(item).strip()],
            tags=[str(item).strip() for item in tags if str(item).strip()],
            author=str(data.get("author", "未署名")).strip() or "未署名",
            video_urls=[str(item).strip() for item in video_urls if str(item).strip()],
        )


class DFKnowledgeBase:
    """DF 本地词条库，支持单文件和 entries/ 多文件目录。"""

    def __init__(self, json_path: Path, entries_dir: Optional[Path] = None):
        self.json_path = json_path
        self.entries_dir = entries_dir
        self.entries: List[DFEntry] = []
        self.aliases: Dict[str, str] = {}
        self.loaded_files: List[Path] = []
        self.loaded_at = 0.0
        self.reload()

    def _parse_payload(self, data: Any, source: Path) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
        """解析单个 JSON 文件，支持单词条、词条列表、标准库文件三种格式。"""
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)], {}

        if not isinstance(data, dict):
            logger.warning(f"DF Helper: skip invalid JSON root in {source}, root must be dict or list.")
            return [], {}

        aliases_raw = data.get("aliases", {})
        if not isinstance(aliases_raw, dict):
            logger.warning(f"DF Helper: aliases in {source} is not a dict, ignored.")
            aliases_raw = {}

        if "entries" in data:
            entries_raw = data.get("entries", [])
            if not isinstance(entries_raw, list):
                logger.warning(f"DF Helper: entries in {source} is not a list, ignored.")
                entries_raw = []
            return [item for item in entries_raw if isinstance(item, dict)], aliases_raw

        if any(key in data for key in ("id", "title", "name", "keywords", "answer", "images", "image")):
            return [data], aliases_raw

        return [], aliases_raw

    def _merge_aliases(self, aliases_raw: Dict[str, str]) -> None:
        for key, value in aliases_raw.items():
            if str(key).strip() and str(value).strip():
                self.aliases[normalize_text(key)] = str(value).strip()

    def _load_entries_from_payload(self, data: Any, source: Path) -> None:
        entries_raw, aliases_raw = self._parse_payload(data, source)
        self._merge_aliases(aliases_raw)

        for item in entries_raw:
            entry = DFEntry.from_dict(item)
            if entry.title:
                # 记录词条来源文件，用于 /df help 按 entries/ 下的文件夹自动归类。
                setattr(entry, "source_path", str(source))
                self.entries.append(entry)
            else:
                logger.warning(f"DF Helper: skip entry without title in {source}: {item}")

    def _iter_json_sources(self) -> List[Path]:
        """只扫描插件目录 entries/**/*.json。"""
        sources: List[Path] = []
        seen: set[str] = set()

        def add_file(path: Path) -> None:
            if path.name in {"_category.json", "category.json"}:
                return
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path)
            if key in seen:
                return
            seen.add(key)
            sources.append(path)

        entries_root = PLUGIN_DIR / "entries"
        if entries_root.exists():
            for path in sorted(entries_root.rglob("*.json")):
                add_file(path)

        return sources

    def reload(self) -> None:
        """
        一次性加载所有分散 JSON 到内存索引。

        加载后：
        - self.entries 是所有文件合并后的词条列表；
        - self.aliases 是所有文件合并后的别名表；
        - 查询时只在内存里模糊匹配，不会再临时打开单个 JSON。
        """
        self.entries = []
        self.aliases = {}
        self.loaded_files = []

        for path in self._iter_json_sources():
            data = read_json_file(path, default=None)
            if data is None:
                continue

            self._load_entries_from_payload(data, path)
            self.loaded_files.append(path)

        deduped: Dict[str, DFEntry] = {}
        for entry in self.entries:
            deduped[entry.id] = entry
        self.entries = list(deduped.values())

        self.loaded_at = time.time()
        logger.info(
            f"DF Helper: loaded memory index: "
            f"{len(self.entries)} entries, "
            f"{len(self.aliases)} aliases, "
            f"{len(self.loaded_files)} JSON files."
        )

    def expand_alias(self, query: str) -> str:
        """展开别名。"""
        normalized = normalize_text(query)
        return self.aliases.get(normalized, query)

    def score_entry(self, query: str, entry: DFEntry) -> Tuple[float, str]:
        """计算查询词与词条的匹配分。"""
        normalized_query = normalize_text(self.expand_alias(query))
        if not normalized_query:
            return 0.0, ""

        candidates: List[Tuple[str, str, float]] = [("标题", entry.title, 1.00)]

        for keyword in entry.keywords:
            candidates.append(("关键词", keyword, 1.05))

        for tag in entry.tags:
            candidates.append(("标签", tag, 0.75))

        if entry.author and entry.author != "未署名":
            candidates.append(("作者", entry.author, 0.60))

        best_score = 0.0
        best_reason = ""

        for field_name, value, weight in candidates:
            normalized_value = normalize_text(value)
            if not normalized_value:
                continue

            if normalized_query == normalized_value:
                score = 100.0 * weight
                reason = f"{field_name}完全匹配：{value}"
            elif normalized_query in normalized_value:
                ratio_bonus = min(
                    len(normalized_query) / max(len(normalized_value), 1),
                    1.0,
                ) * 12
                score = (82.0 + ratio_bonus) * weight
                reason = f"{field_name}包含查询词：{value}"
            elif normalized_value in normalized_query:
                ratio_bonus = min(
                    len(normalized_value) / max(len(normalized_query), 1),
                    1.0,
                ) * 10
                score = (78.0 + ratio_bonus) * weight
                reason = f"查询词包含{field_name}：{value}"
            else:
                ratio = SequenceMatcher(
                    None,
                    normalized_query,
                    normalized_value,
                ).ratio()
                score = ratio * 100.0 * weight
                reason = f"{field_name}相似：{value}"

            if score > best_score:
                best_score = min(score, 100.0)
                best_reason = reason

        return best_score, best_reason

    def search(self, query: str, top_k: int, threshold: float) -> List[Dict[str, Any]]:
        """搜索词条。"""
        results: List[Dict[str, Any]] = []
        for entry in self.entries:
            score, reason = self.score_entry(query, entry)
            if score >= threshold:
                results.append(
                    {
                        "entry": entry,
                        "score": round(score, 1),
                        "reason": reason,
                    }
                )

        results.sort(key=lambda item: item["score"], reverse=True)
        return results[:top_k]

    def suggest(self, query: str, top_k: int) -> List[Dict[str, Any]]:
        """低阈值候选，用于未命中时提示。"""
        results: List[Dict[str, Any]] = []
        for entry in self.entries:
            score, reason = self.score_entry(query, entry)
            if score > 20:
                results.append(
                    {
                        "entry": entry,
                        "score": round(score, 1),
                        "reason": reason,
                    }
                )

        results.sort(key=lambda item: item["score"], reverse=True)
        return results[:top_k]



class HelpCatalog:
    """DF 帮助分类目录，支持总文件和 help/ 分散分类文件。"""

    def __init__(self, catalog_file: Path, help_dir: Path):
        self.catalog_file = catalog_file
        self.help_dir = help_dir
        self.title = "DF 查询分类帮助"
        self.description = ""
        self.categories: List[Dict[str, Any]] = []
        self.loaded_files: List[Path] = []
        self.reload()

    def _normalize_category(self, data: Dict[str, Any], source: Path) -> Optional[Dict[str, Any]]:
        if not isinstance(data, dict):
            logger.warning(f"DF Helper: invalid help category in {source}, ignored.")
            return None

        title = str(data.get("title") or data.get("name") or data.get("id") or "").strip()
        if not title:
            logger.warning(f"DF Helper: help category without title in {source}, ignored.")
            return None

        items = data.get("items", [])
        if not isinstance(items, list):
            items = []

        normalized_items = []
        for item in items:
            if isinstance(item, str):
                normalized_items.append({"title": item, "query": item, "desc": ""})
            elif isinstance(item, dict):
                normalized_items.append(item)

        result = dict(data)
        result["title"] = title
        result["items"] = normalized_items
        return result

    def _load_payload(self, data: Any, source: Path) -> None:
        if isinstance(data, list):
            for item in data:
                category = self._normalize_category(item, source)
                if category:
                    self.categories.append(category)
            return

        if not isinstance(data, dict):
            logger.warning(f"DF Helper: invalid help JSON root in {source}, ignored.")
            return

        if "categories" in data:
            self.title = str(data.get("title") or self.title)
            self.description = str(data.get("description") or self.description)
            categories = data.get("categories", [])
            if not isinstance(categories, list):
                logger.warning(f"DF Helper: categories in {source} is not a list, ignored.")
                return
            for item in categories:
                category = self._normalize_category(item, source)
                if category:
                    self.categories.append(category)
            return

        category = self._normalize_category(data, source)
        if category:
            self.categories.append(category)

    def _iter_sources(self) -> List[Path]:
        sources: List[Path] = []

        if self.catalog_file.exists():
            sources.append(self.catalog_file)
        else:
            logger.info(f"DF Helper: optional help catalog not found, skipped: {self.catalog_file}")

        if self.help_dir.exists():
            for path in sorted(self.help_dir.rglob("*.json")):
                try:
                    if self.catalog_file.exists() and path.resolve() == self.catalog_file.resolve():
                        continue
                except OSError as exc:
                    logger.debug(f"DF Helper: resolve help path failed: {path}, {exc}")
                sources.append(path)

        return sources

    def reload(self) -> None:
        self.title = "DF 查询分类帮助"
        self.description = ""
        self.categories = []
        self.loaded_files = []

        for path in self._iter_sources():
            data = read_json_file(path, default=None)
            if data is None:
                continue
            self._load_payload(data, path)
            self.loaded_files.append(path)

        deduped: Dict[str, Dict[str, Any]] = {}
        for index, category in enumerate(self.categories):
            key = str(category.get("id") or category.get("title") or index)
            deduped[key] = category
        self.categories = list(deduped.values())

        logger.info(
            f"DF Helper: loaded help catalog: "
            f"{len(self.categories)} categories from {len(self.loaded_files)} JSON files."
        )


@register(
    PLUGIN_NAME,
    "GuHuiHeXiaoDao",
    "Dwarf Fortress / 矮人要塞攻略查询助手",
    PLUGIN_VERSION,
    "",
)
class DFHelperPlugin(Star):
    """AstrBot 插件主体。"""

    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None):
        super().__init__(context)
        self.config = config or AstrBotConfig({})

        self.data_dir = get_plugin_data_dir()
        self.data_file = resolve_data_file(
            str(self.config.get("data_file", "df_entries.json")),
            self.data_dir,
        )
        self.entries_dir = Path(
            str(self.config.get("entries_dir", "entries"))
        ).expanduser()
        self.image_dir = Path(
            str(self.config.get("image_dir", "assets/images"))
        ).expanduser()
        self.help_catalog_file = resolve_data_file(
            str(self.config.get("help_catalog_file", "help_catalog.json")),
            self.data_dir,
        )
        self.help_dir = Path(
            str(self.config.get("help_dir", "help"))
        ).expanduser()

        if not self.entries_dir.is_absolute():
            self.entries_dir = self.data_dir / self.entries_dir

        if not self.image_dir.is_absolute():
            self.image_dir = self.data_dir / self.image_dir

        if not self.help_dir.is_absolute():
            self.help_dir = self.data_dir / self.help_dir

        self.threshold = float(self.config.get("match_threshold", 55))
        self.top_k = int(self.config.get("top_k", 5))
        self.max_images = int(self.config.get("max_images", 30))
        self.forward_node_name = str(self.config.get("forward_node_name", "DF Helper"))
        self.forward_node_uin = str(self.config.get("forward_node_uin", "10000"))

        ensure_initial_data(
            self.data_file,
            self.entries_dir,
            self.image_dir,
            self.help_catalog_file,
            self.help_dir,
        )
        self.kb = DFKnowledgeBase(self.data_file, self.entries_dir)
        self.help_catalog = HelpCatalog(self.help_catalog_file, self.help_dir)

    async def _handle_df_query(self, event: AstrMessageEvent, query: str):
        """
        处理 /df 查询。

        要求：
        - 最终输出只有一条聊天记录；
        - 成功后不 yield 普通文字/图片；
        - 失败时只给一条错误提示。
        """
        query = (query or "").strip()
        if not query:
            return

        command = normalize_text(query)
        if command in {"reload", "重载", "刷新"}:
            self.kb.reload()
            self.help_catalog.reload()
            yield event.plain_result(
                f"已重载 DF 词条库：{len(self.kb.entries)} 个词条，"
                f"{len(self.kb.loaded_files)} 个词条 JSON，"
                f"{len(self.help_catalog.categories)} 个帮助分类。"
            )
            return

        results = self.kb.search(query, top_k=self.top_k, threshold=self.threshold)
        if not results:
            suggestions = self.kb.suggest(query, top_k=self.top_k)
            if suggestions:
                msg = "没有达到命中阈值。你是不是想查：\n"
                for index, item in enumerate(suggestions, 1):
                    entry: DFEntry = item["entry"]
                    msg += f"{index}. {entry.title}（相似度 {item['score']}）\n"
                msg += f"\n当前阈值：{self.threshold}。"
                yield event.plain_result(msg.strip())
            else:
                yield event.plain_result(
                    f"没有找到与「{query}」相关的 DF 词条。\n"
                    "请检查 entries/ 下的词条 JSON 后发送 /df reload。"
                )
            return

        best = results[0]
        entry: DFEntry = best["entry"]
        near = [
            item for item in results[1:]
            if item["score"] >= max(self.threshold, best["score"] - 8)
        ]

        # 合并转发节点名称按词条 author 显示；uin 仍使用机器人 QQ，避免头像异常。
        _, node_uin = await self._get_bot_forward_identity(event)
        nodes = self._build_entry_forward_nodes(
            entry,
            score=best["score"],
            reason=best["reason"],
            near=near,
            node_name=entry.author,
            node_uin=node_uin,
        )

        sent = await self._send_onebot_forward(event, nodes)
        if not sent:
            logger.warning("DF Helper: entry forward failed.")
            yield event.plain_result(
                "词条已命中，但聊天记录转发发送失败。请检查 NapCat/OneBot 日志。"
            )
            return

        if entry.video_urls:
            yield event.plain_result(self._format_video_links(entry.video_urls))

    def _format_entry_list(self) -> str:
        """格式化词条列表。"""
        if not self.kb.entries:
            return "当前词条库为空。"

        lines = [f"DF 词条列表（共 {len(self.kb.entries)} 个）："]
        for index, entry in enumerate(self.kb.entries[:80], 1):
            keywords = "、".join(entry.keywords[:5])
            author = f"｜作者：{entry.author}" if entry.author else ""
            suffix = f"｜关键词：{keywords}" if keywords else ""
            lines.append(f"{index}. {entry.title}{author}{suffix}")

        if len(self.kb.entries) > 80:
            lines.append(f"... 还有 {len(self.kb.entries) - 80} 个未显示。")

        lines.append(f"\n全局别名文件（可选）：{self.data_file}")
        lines.append(f"拆分词条目录：{self.entries_dir}")
        lines.append(f"图片目录：{self.image_dir}")
        lines.append(f"兼容旧图片目录：{PLUGIN_DIR / 'assets' / 'images'}")
        lines.append(f"兼容旧数据图片目录：{PLUGIN_DIR / 'data' / 'assets' / 'images'}")
        lines.append(f"已一次性加载 JSON 文件数：{len(self.kb.loaded_files)}")
        for path in self.kb.loaded_files[:10]:
            lines.append(f"- {path}")
        if len(self.kb.loaded_files) > 10:
            lines.append(f"... 还有 {len(self.kb.loaded_files) - 10} 个文件未显示。")
        return "\n".join(lines)

    def _format_help_text(self) -> str:
        """格式化帮助文本。普通文本降级时使用，不显示本地路径。"""
        return (
            "DF 查询用法：\n"
            "/df 铁矿\n"
            "/df 铜矿\n"
            "/df reload\n"
            "/df help\n\n"
            f"当前已加载 {len(self.kb.entries)} 个词条。"
        )

    def _format_video_links(self, video_urls: List[str]) -> str:
        """格式化视频链接。"""
        if len(video_urls) == 1:
            return f"视频链接：\n{video_urls[0]}"

        lines = ["视频链接："]
        for index, url in enumerate(video_urls, 1):
            lines.append(f"{index}. {url}")
        return "\n".join(lines)

    def _resolve_image_path(self, image: str) -> Optional[Path | str]:
        """
        解析图片路径。

        JSON 中建议只写文件名，例如：
        "images": ["iron_1.jpg", "iron_2.jpg"]

        本地图片固定读取：
        assets/images/
        """
        image = (image or "").strip()
        if not image:
            return None

        if image.startswith(("http://", "https://")):
            return image

        if image.startswith("file://"):
            path = Path(image[7:]).expanduser()
            return path if path.exists() else None

        path = Path(image).expanduser()
        if path.is_absolute():
            return path if path.exists() else None

        candidate = PLUGIN_DIR / "assets" / "images" / image
        return candidate if candidate.exists() else None

    def _image_to_onebot_file(self, image: str) -> Optional[str]:
        """
        转成 OneBot image file 字段。

        本地图片：
        - AstrBot 从 assets/images 读取；
        - 转成 base64://；
        - NapCat 不再需要读取本地路径。

        URL 图片：
        - 原样返回。
        """
        resolved = self._resolve_image_path(image)
        if resolved is None:
            return None

        if isinstance(resolved, str):
            return resolved

        try:
            raw = resolved.read_bytes()
            encoded = base64.b64encode(raw).decode("ascii")
            return "base64://" + encoded
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"DF Helper: encode image failed: {resolved}: {exc}")
            return None

    def _get_limited_images(self, entry: DFEntry) -> List[str]:
        """限制单词条进入合并转发的图片数量。"""
        if self.max_images <= 0:
            return entry.images[:]
        return entry.images[: self.max_images]

    async def _get_bot_forward_identity(self, event: AstrMessageEvent) -> Tuple[str, str]:
        """
        获取机器人自己的昵称和 QQ 号。

        OneBot 合并转发节点：
        - data.name 决定显示名称；
        - data.uin 决定头像。
        """
        fallback_name = self.forward_node_name or "DF Helper"
        fallback_uin = self.forward_node_uin or "10000"

        try:
            bot = getattr(event, "bot", None)
            api = getattr(bot, "api", None)
            if api is not None:
                info = await api.call_action("get_login_info")
                if isinstance(info, dict):
                    data = info.get("data", info)
                    nickname = str(
                        data.get("nickname")
                        or data.get("name")
                        or data.get("user_name")
                        or ""
                    ).strip()
                    user_id = str(
                        data.get("user_id")
                        or data.get("uin")
                        or data.get("qq")
                        or ""
                    ).strip()
                    if user_id:
                        return (nickname or fallback_name), user_id
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"DF Helper: get_login_info failed: {exc}")

        for obj in (event, getattr(event, "message_obj", None)):
            if obj is None:
                continue

            for attr_name in ("self_id", "bot_id", "account_id", "login_id"):
                value = getattr(obj, attr_name, None)
                if value:
                    return fallback_name, str(value)

            for method_name in ("get_self_id", "get_bot_id"):
                method = getattr(obj, method_name, None)
                if callable(method):
                    try:
                        value = method()
                        if value:
                            return fallback_name, str(value)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(f"DF Helper: {method_name} failed: {exc}")

        return fallback_name, fallback_uin

    def _make_node(
        self,
        content: Any,
        name: Optional[str] = None,
        uin: Optional[str] = None,
    ) -> Dict[str, Any]:
        """构造 OneBot 合并转发节点。"""
        return {
            "type": "node",
            "data": {
                "name": name or self.forward_node_name,
                "uin": str(uin or self.forward_node_uin),
                "content": content,
            },
        }

    def _build_entry_forward_nodes(
        self,
        entry: DFEntry,
        score: float,
        reason: str,
        near: Optional[List[Dict[str, Any]]] = None,
        node_name: Optional[str] = None,
        node_uin: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        构造 /df 查询词的一级合并转发节点。

        最终只发送一条聊天记录：
        - 节点 1：文字说明；
        - 节点 2..N：按 JSON images 数组顺序逐张发送图片；
        - 节点名称：优先使用词条 author。

        注意：这里不把“可能相关”塞进命中的合并转发，避免查询成功后出现额外无关节点。
        """
        nodes: List[Dict[str, Any]] = []
        tag_text = f"｜{'、'.join(entry.tags)}" if entry.tags else ""
        author_name = (entry.author or node_name or self.forward_node_name or "DF Helper").strip()

        body = (
            f"【DF 查询】{entry.title}{tag_text}\n"
            f"作者：{entry.author}\n"
            f"匹配度：{score}｜{reason}\n\n"
            f"{entry.answer or '该词条还没有填写文字说明。'}"
        )
        nodes.append(
            self._make_node(
                [{"type": "text", "data": {"text": body}}],
                name=author_name,
                uin=node_uin,
            )
        )

        for index, image in enumerate(self._get_limited_images(entry), 1):
            file_value = self._image_to_onebot_file(image)
            if file_value:
                content = [
                    {"type": "text", "data": {"text": f"图 {index}\n"}},
                    {"type": "image", "data": {"file": file_value}},
                ]
            else:
                content = [
                    {"type": "text", "data": {"text": f"[图片缺失] {image}"}}
                ]
            nodes.append(self._make_node(content, name=author_name, uin=node_uin))

        if self.max_images > 0 and len(entry.images) > self.max_images:
            nodes.append(
                self._make_node(
                    [
                        {
                            "type": "text",
                            "data": {
                                "text": f"图片过多，已发送前 {self.max_images} 张，共 {len(entry.images)} 张。"
                            },
                        }
                    ],
                    name=author_name,
                    uin=node_uin,
                )
            )


        return nodes

    def _build_entry_fallback_chain(
        self,
        entry: DFEntry,
        score: float,
        reason: str,
        near: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Any]:
        """合并转发失败时的普通消息链降级。"""
        chain: List[Any] = []
        tag_text = f"｜{'、'.join(entry.tags)}" if entry.tags else ""

        text = (
            f"【DF 查询】{entry.title}{tag_text}\n"
            f"作者：{entry.author}\n"
            f"匹配度：{score}｜{reason}\n\n"
            f"{entry.answer or '该词条还没有填写文字说明。'}"
        )
        chain.append(Comp.Plain(text))

        for image in self._get_limited_images(entry):
            resolved = self._resolve_image_path(image)
            if resolved is None:
                continue

            try:
                if isinstance(resolved, str):
                    chain.append(Comp.Image.fromURL(resolved))
                elif resolved.exists():
                    chain.append(Comp.Image.fromFileSystem(str(resolved)))
                else:
                    chain.append(Comp.Plain(f"\n[图片缺失] {resolved}"))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"DF Helper: send fallback image failed: {exc}")
                chain.append(Comp.Plain(f"\n[图片发送失败] {image}: {exc}"))

        if near:
            text_near = "\n\n可能相关：\n"
            for item in near[:4]:
                near_entry: DFEntry = item["entry"]
                text_near += f"- {near_entry.title}（{item['score']}）\n"

            chain.append(Comp.Plain(text_near.strip()))

        return chain

    def _entries_root_candidates(self) -> List[Path]:
        """只使用插件目录 entries/ 作为 /df help 分类根目录。"""
        root = PLUGIN_DIR / "entries"
        return [root] if root.exists() else []

    def _read_category_meta(self, folder: Path) -> Dict[str, Any]:
        """读取 _category.json / category.json。"""
        for name in ("_category.json", "category.json"):
            path = folder / name
            if not path.exists():
                continue
            data = read_json_file(path, default={})
            if isinstance(data, dict):
                return data
        return {}

    def _category_title_from_folder(self, folder_key: str, meta: Dict[str, Any]) -> str:
        title = str(meta.get("title") or meta.get("name") or "").strip()
        if title:
            return title
        return folder_key.replace("_", " ").replace("-", " ").strip() or "未分类"

    def _folder_key_for_entry(self, entry: DFEntry) -> str:
        source_path = getattr(entry, "source_path", "")
        if not source_path:
            return "未分类"
        try:
            source = Path(source_path).resolve()
        except OSError:
            source = Path(source_path)
        for root in self._entries_root_candidates():
            try:
                rel = source.relative_to(root.resolve())
                if len(rel.parts) >= 2:
                    return rel.parts[0]
                return "未分类"
            except (ValueError, OSError):
                continue
        return "未分类"

    def _folder_path_for_key(self, folder_key: str) -> Path:
        if folder_key == "未分类":
            for root in self._entries_root_candidates():
                if root.exists():
                    return root
            return self.entries_dir
        for root in self._entries_root_candidates():
            candidate = root / folder_key
            if candidate.exists():
                return candidate
        return self.entries_dir / folder_key

    def _build_auto_help_categories(self) -> List[Dict[str, Any]]:
        grouped: Dict[str, List[DFEntry]] = {}
        for entry in self.kb.entries:
            folder_key = self._folder_key_for_entry(entry)
            grouped.setdefault(folder_key, []).append(entry)

        for root in self._entries_root_candidates():
            if not root.exists():
                continue
            for child in sorted(root.iterdir()):
                if not child.is_dir():
                    continue
                if (child / "_category.json").exists() or (child / "category.json").exists():
                    grouped.setdefault(child.name, [])

        categories: List[Dict[str, Any]] = []
        for folder_key, entries in grouped.items():
            folder = self._folder_path_for_key(folder_key)
            meta = self._read_category_meta(folder)
            items = []
            for entry in sorted(entries, key=lambda item: item.title):
                query = entry.keywords[0] if entry.keywords else entry.title
                items.append({
                    "title": entry.title,
                    "query": query,
                    "keywords": entry.keywords[:6],
                    "desc": render_text(entry.answer).replace("\n", " ").strip()[:80],
                })
            categories.append({
                "id": folder_key,
                "title": self._category_title_from_folder(folder_key, meta),
                "summary": str(meta.get("summary") or meta.get("description") or "").strip(),
                "order": int(meta.get("order", 999)),
                "items": items,
            })
        categories.sort(key=lambda item: (item.get("order", 999), str(item.get("title", ""))))
        return categories

    def _format_help_category_header(self, category: Dict[str, Any]) -> str:
        """分类标题文本，不显示本地路径。"""
        title = render_text(category.get("title") or "未命名分类").strip()
        return f"名称：{title}"

    def _iter_help_category_items(self, category: Dict[str, Any]) -> List[Dict[str, str]]:
        """
        提取分类下的条目。

        每个条目会成为该分类独立合并转发里的一个消息节点。
        """
        items = category.get("items", [])
        result: List[Dict[str, str]] = []

        if not isinstance(items, list):
            return result

        for item in items:
            if isinstance(item, str):
                query = render_text(item).strip()
                if query:
                    result.append({"title": query, "query": query})
                continue

            if not isinstance(item, dict):
                continue

            title = render_text(item.get("title") or item.get("name") or item.get("query") or "").strip()
            query = render_text(item.get("query") or title).strip()

            if query:
                result.append({"title": title or query, "query": query})

        return result

    def _format_help_item(self, item: Dict[str, str]) -> str:
        """
        单个帮助条目节点。

        只显示标题和可复制指令，不显示描述、关键词和路径。
        """
        title = item.get("title", "").strip()
        query = item.get("query", "").strip()

        if title and title != query:
            return f"{title}\n/df {query}"

        return f"/df {query}"

    def _get_help_categories(self) -> List[Dict[str, Any]]:
        """
        获取最终帮助分类。

        来源：
        - entries/ 一级文件夹自动归类；
        - help_catalog.json / help/*.json 作为额外分类；
        - 如果 id/title 重复，优先使用 entries/ 自动分类。
        """
        auto_categories = self._build_auto_help_categories()
        extra_categories = list(self.help_catalog.categories)

        auto_ids = {str(item.get("id") or item.get("title") or "") for item in auto_categories}
        filtered_extra = [
            item for item in extra_categories
            if str(item.get("id") or item.get("title") or "") not in auto_ids
        ]

        return auto_categories + filtered_extra

    def _build_help_overview_nodes(
        self,
        categories: List[Dict[str, Any]],
        node_name: Optional[str] = None,
        node_uin: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        构造 /df help 的总览合并转发。

        不显示任何本地路径。
        """
        overview = (
            "【DF 查询分类帮助】\n"
            "使用方式：\n"
            "/df 铁矿\n"
            "/df 铜矿\n"
            "/df reload\n"
            "/df help\n\n"
            f"分类：{len(categories)} 个\n"
            f"词条：{len(self.kb.entries)} 个\n\n"
            "说明：每个分类会再单独发送一条聊天记录。"
        )

        nodes = [
            self._make_node(
                overview,
                name=node_name,
                uin=node_uin,
            )
        ]

        for category in categories:
            nodes.append(
                self._make_node(
                    self._format_help_category_header(category),
                    name=node_name,
                    uin=node_uin,
                )
            )

        if not categories:
            nodes.append(
                self._make_node(
                    "当前没有可显示的帮助分类。\n请在 entries/ 下创建分类文件夹并放入词条 JSON。",
                    name=node_name,
                    uin=node_uin,
                )
            )

        return nodes

    def _build_category_forward_nodes(
        self,
        category: Dict[str, Any],
        node_name: Optional[str] = None,
        node_uin: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        构造单个分类的独立合并转发。

        这就是“二级目录”的实际呈现：
        - 每个大分类单独发送一条合并转发；
        - 分类内每个词条是该合并转发内的一条消息节点。
        """
        nodes: List[Dict[str, Any]] = [
            self._make_node(
                self._format_help_category_header(category),
                name=node_name,
                uin=node_uin,
            )
        ]

        items = self._iter_help_category_items(category)
        if not items:
            nodes.append(
                self._make_node(
                    "暂无词条。",
                    name=node_name,
                    uin=node_uin,
                )
            )
            return nodes

        for item in items:
            nodes.append(
                self._make_node(
                    self._format_help_item(item),
                    name=node_name,
                    uin=node_uin,
                )
            )

        return nodes

    def _build_help_forward_nodes(
        self,
        node_name: Optional[str] = None,
        node_uin: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        /df help 自动嵌套合并转发。

        不依赖 resid / forward_id；每次自动构造：
        一级合并转发 -> 分类节点 content = 子 node 列表。
        在当前 NapCat/QQ 上可能显示为 [卡片消息]，这是你要求回退的状态。
        """
        categories = self._get_help_categories()
        overview_text = (
            "【DF 查询分类帮助】\n"
            "使用方式：\n"
            "/df 铁矿\n"
            "/df 铜矿\n"
            "/df reload\n"
            "/df help\n\n"
            f"分类：{len(categories)} 个\n"
            f"词条：{len(self.kb.entries)} 个"
        )
        nodes: List[Dict[str, Any]] = [self._make_node(overview_text, name=node_name, uin=node_uin)]

        for category in categories:
            child_nodes: List[Dict[str, Any]] = [
                self._make_node(self._format_help_category_header(category), name=node_name, uin=node_uin)
            ]
            items = self._iter_help_category_items(category)
            if not items:
                child_nodes.append(self._make_node("暂无词条。", name=node_name, uin=node_uin))
            else:
                for item in items:
                    child_nodes.append(self._make_node(self._format_help_item(item), name=node_name, uin=node_uin))

            nodes.append(self._make_node(child_nodes, name=node_name, uin=node_uin))

        if not categories:
            nodes.append(self._make_node("当前没有可显示的帮助分类。\n请在 entries/ 下创建分类文件夹并放入词条 JSON。", name=node_name, uin=node_uin))
        return nodes

    def _get_group_id_from_event(self, event: AstrMessageEvent) -> Optional[str]:
        """尽量从事件对象中获取群号。"""
        for obj in (event, getattr(event, "message_obj", None)):
            if obj is None:
                continue

            for attr_name in ("group_id", "group"):
                value = getattr(obj, attr_name, None)
                if value:
                    return str(value)

            method = getattr(obj, "get_group_id", None)
            if callable(method):
                try:
                    value = method()
                    if value:
                        return str(value)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"DF Helper: get_group_id failed: {exc}")

        unified_origin = str(getattr(event, "unified_msg_origin", "") or "")
        match = re.search(r"(?:group|群|/)(\d{5,})", unified_origin, re.IGNORECASE)
        if match:
            return match.group(1)

        return None

    async def _send_onebot_forward(
        self,
        event: AstrMessageEvent,
        nodes: List[Dict[str, Any]],
    ) -> bool:
        """
        发送 OneBot 合并转发。

        只负责发送一条聊天记录。
        如果失败，返回 False，不把节点展开成普通消息。
        """
        try:
            bot = getattr(event, "bot", None)
            api = getattr(bot, "api", None)
            if api is None:
                logger.warning("DF Helper: OneBot api unavailable.")
                return False

            group_id = self._get_group_id_from_event(event)
            user_id = str(event.get_sender_id())

            if group_id:
                action = "send_group_forward_msg"
                base_payload: Dict[str, Any] = {"group_id": int(group_id)}
            else:
                action = "send_private_forward_msg"
                base_payload = {"user_id": int(user_id)}

            last_error: Optional[Exception] = None
            last_response: Any = None

            # NapCat 通常吃 messages；部分实现吃 nodes，所以保留双尝试。
            for field_name in ("messages", "nodes"):
                try:
                    payload = dict(base_payload)
                    payload[field_name] = nodes
                    logger.info(
                        f"DF Helper: call {action} with {field_name}, nodes={len(nodes)}"
                    )
                    response = await api.call_action(action, **payload)
                    last_response = response
                    logger.info(f"DF Helper: forward response: {response}")
                    return True
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    logger.warning(
                        f"DF Helper: forward failed with field={field_name}: {exc}"
                    )

            logger.warning(
                f"DF Helper: forward send failed, last_error={last_error}, last_response={last_response}"
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"DF Helper: _send_onebot_forward error: {exc}\n"
                f"{traceback.format_exc()}"
            )
            return False

    async def _send_forward_help(self, event: AstrMessageEvent) -> bool:
        """
        发送 /df help。

        只发送一条合并转发聊天记录。
        这条聊天记录内部包含总览、分类标题、分类下的词条指令节点。
        不显示任何服务器本地路径。
        """
        node_name, node_uin = await self._get_bot_forward_identity(event)
        nodes = self._build_help_forward_nodes(node_name=node_name, node_uin=node_uin)
        return await self._send_onebot_forward(event, nodes)

    async def _send_entry_forward(
        self,
        event: AstrMessageEvent,
        entry: DFEntry,
        score: float,
        reason: str,
        near: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """发送词条合并转发。"""
        _, node_uin = await self._get_bot_forward_identity(event)
        nodes = self._build_entry_forward_nodes(
            entry,
            score,
            reason,
            near,
            node_name=entry.author,
            node_uin=node_uin,
        )
        return await self._send_onebot_forward(event, nodes)

    if EventMessageType is not None:

        @filter.event_message_type(EventMessageType.ALL)
        async def df_listener(self, event: AstrMessageEvent):
            """全消息监听入口。"""
            raw = get_current_message_text(event)
            if not raw:
                return

            if is_df_help(raw):
                stop_event_safely(event)
                sent = await self._send_forward_help(event)
                if not sent:
                    yield event.plain_result(self._format_help_text())
                return

            if not is_df_command_line(raw):
                return

            stop_event_safely(event)
            query = split_df_query(raw)

            async for result in self._handle_df_query(event, query):
                yield result

    @filter.command("df")
    async def df_command_fallback(self, event: AstrMessageEvent):
        """EventMessageType 不存在时的兜底命令入口。"""
        if EventMessageType is not None:
            return

        raw = get_current_message_text(event)
        if is_df_help(raw):
            stop_event_safely(event)
            sent = await self._send_forward_help(event)
            if not sent:
                yield event.plain_result(self._format_help_text())
            return

        if not is_df_command_line(raw):
            return

        stop_event_safely(event)
        query = split_df_query(raw)

        async for result in self._handle_df_query(event, query):
            yield result

    async def terminate(self):
        """插件卸载时调用。"""
        logger.info("DF Helper: terminated.")
