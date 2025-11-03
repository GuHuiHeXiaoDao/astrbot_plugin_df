# -*- coding: utf-8 -*-
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import os, re, json, urllib.parse, traceback

import requests
import yaml

import astrbot.api.message_components as Comp
from astrbot.api import logger, AstrBotConfig, FunctionTool
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.star import Context, Star, register

PLUGIN_DIR = os.path.dirname(__file__)

def _abspath(*p):
    return os.path.join(PLUGIN_DIR, *p)

def _load_json(path: str, default: Any):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

class ContentEntry:
    def __init__(self, key: str, aliases: List[str], blocks: List[Dict[str, Any]]):
        self.key = key
        self.aliases = aliases or []
        self.blocks = blocks

class ContentPack:
    def __init__(self, pack_dir: str):
        self.pack_dir = pack_dir
        self.entries_dir = os.path.join(pack_dir, "entries")
        self.assets_dir = os.path.join(pack_dir, "assets")
        self.by_key: Dict[str, ContentEntry] = {}
        self.alias: Dict[str, str] = {}
        self.reload()

    @staticmethod
    def _norm(s: str) -> str:
        return s.strip().lower()

    def reload(self):
        self.by_key.clear()
        self.alias.clear()
        if not os.path.isdir(self.entries_dir):
            return
        for fn in os.listdir(self.entries_dir):
            path = os.path.join(self.entries_dir, fn)
            if not os.path.isfile(path):
                continue
            try:
                if fn.endswith(".md"):
                    entry = self._load_md(path)
                elif fn.endswith(".json"):
                    entry = self._load_json_entry(path)
                else:
                    continue
                if entry:
                    k = self._norm(entry.key)
                    self.by_key[k] = entry
                    self.alias[k] = k
                    for a in entry.aliases:
                        self.alias[self._norm(a)] = k
            except Exception as e:
                logger.error(f"Load entry failed: {path} -> {e}")

    def _split_front_matter(self, text: str):
        if text.startswith("---"):
            import re, yaml
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
            if m:
                try:
                    front = yaml.safe_load(m.group(1)) or {}
                except Exception:
                    front = {}
                body = m.group(2)
                return front, body
        return {}, text

    def _parse_md_blocks(self, body: str) -> List[Dict[str, Any]]:
        blocks: List[Dict[str, Any]] = []
        pos = 0
        pattern = re.compile(r'!\[[^\]]*\]\(([^)]+)\)')
        for m in pattern.finditer(body):
            start, end = m.span()
            img_src = m.group(1).strip()
            txt = body[pos:start].strip()
            if txt:
                blocks.append({"type": "text", "content": txt})
            blocks.append({"type": "image", "src": img_src})
            pos = end
        tail = body[pos:].strip()
        if tail:
            blocks.append({"type": "text", "content": tail})
        return blocks or [{"type": "text", "content": body.strip()}]

    def _load_md(self, path: str) -> Optional[ContentEntry]:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        meta, body = self._split_front_matter(text)
        key = meta.get("key") or os.path.splitext(os.path.basename(path))[0]
        aliases = meta.get("aliases") or []
        blocks = self._parse_md_blocks(body)
        for src in (meta.get("images") or []):
            blocks.append({"type": "image", "src": str(src)})
        return ContentEntry(key=key, aliases=aliases, blocks=blocks)

    def _load_json_entry(self, path: str) -> Optional[ContentEntry]:
        data = _load_json(path, {})
        key = data.get("key") or os.path.splitext(os.path.basename(path))[0]
        aliases = data.get("aliases") or []
        blocks = data.get("blocks") or []
        return ContentEntry(key=key, aliases=aliases, blocks=blocks)

    def match(self, keyword: str) -> Optional[ContentEntry]:
        if not keyword:
            return None
        k = self._norm(keyword)
        if k in self.alias:
            return self.by_key.get(self.alias[k])
        return None

    def render_chain(self, entry: ContentEntry) -> List[Any]:
        chain: List[Any] = []
        for blk in entry.blocks:
            if blk.get("type") == "text":
                content = str(blk.get("content", "")).strip()
                if content:
                    chain.append(Comp.Plain(content))
            elif blk.get("type") == "image":
                src = str(blk.get("src", "")).strip()
                if not src:
                    continue
                if src.startswith("http://") or src.startswith("https://"):
                    chain.append(Comp.Image.fromURL(src))
                else:
                    abs1 = os.path.join(self.entries_dir, src)
                    abs2 = os.path.join(self.assets_dir, src)
                    abs3 = os.path.join(self.pack_dir, src)
                    for candidate in (abs1, abs2, abs3):
                        if os.path.exists(candidate):
                            chain.append(Comp.Image.fromFileSystem(candidate))
                            break
                    else:
                        chain.append(Comp.Plain(f"[提示] 找不到本地图片：{src}"))
        return chain

class KeywordKB:
    def __init__(self, kb_path: str):
        self.kb_path = kb_path
        self.data = _load_json(kb_path, default={"aliases": {}, "entries": {}})
        self.alias = {k.lower(): v for k, v in self.data.get("aliases", {}).items()}
        self.entries = self.data.get("entries", {})

    def normalize(self, key: str) -> str:
        return self.alias.get(key.strip().lower(), key.strip())

    def lookup(self, key: str) -> Optional[Dict[str, Any]]:
        k = self.normalize(key)
        return self.entries.get(k)

class WikiClient:
    def __init__(self, mode: str = "mediawiki", lang: str = "en",
                 fandom_site: str = "", mw_host: str = "dwarffortresswiki.org",
                 mw_https: bool = True, mw_path_prefix: str = ""):
        self.mode = mode
        self.lang = lang
        self.fandom_site = fandom_site
        self.mw_host = mw_host
        self.mw_https = mw_https
        self.mw_path_prefix = mw_path_prefix.strip("/")
        if self.mw_path_prefix:
            self.mw_path_prefix += "/"

    def search(self, query: str, limit: int = 3) -> List[Dict[str, Any]]:
        if self.mode == "wikipedia":
            return self._search_wikipedia(query, limit)
        elif self.mode == "fandom":
            return self._search_fandom(query, limit)
        else:
            return self._search_mediawiki(query, limit)

    def page_summary(self, title: str) -> Tuple[str, str]:
        if self.mode == "wikipedia":
            return self._wikipedia_summary(title)
        elif self.mode == "fandom":
            return self._fandom_summary(title)
        else:
            return self._mediawiki_summary(title)

    def _wp_base(self):
        return f"https://{self.lang}.wikipedia.org/w/api.php"
    def _search_wikipedia(self, query: str, limit: int = 3):
        params = {"action": "query", "list": "search", "srsearch": query, "srlimit": limit, "format": "json"}
        r = requests.get(self._wp_base(), params=params, timeout=10); r.raise_for_status()
        data = r.json().get("query", {}).get("search", [])
        out = []
        for it in data:
            title = it.get("title")
            url = f"https://{self.lang}.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
            out.append({"title": title, "url": url, "snippet": it.get("snippet", "")})
        return out
    def _wikipedia_summary(self, title: str) -> Tuple[str, str]:
        params = {"action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1, "titles": title, "format": "json"}
        r = requests.get(self._wp_base(), params=params, timeout=10); r.raise_for_status()
        pages = r.json().get("query", {}).get("pages", {})
        for _, v in pages.items():
            extract = (v.get("extract", "") or "").strip()
            url = f"https://{self.lang}.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
            return extract[:900], url
        return "", f"https://{self.lang}.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))

    def _fd_host(self):
        host = (self.fandom_site or "www") + ".fandom.com"
        return f"https://{host}/api.php", host
    def _search_fandom(self, query: str, limit: int = 3):
        base, host = self._fd_host()
        params = {"action": "query", "list": "search", "srsearch": query, "srlimit": limit, "format": "json"}
        r = requests.get(base, params=params, timeout=10); r.raise_for_status()
        data = r.json().get("query", {}).get("search", [])
        out = []
        for it in data:
            title = it.get("title")
            url = f"https://{host}/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
            out.append({"title": title, "url": url, "snippet": it.get("snippet", "")})
        return out
    def _fandom_summary(self, title: str) -> Tuple[str, str]:
        base, host = self._fd_host()
        params = {"action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1, "titles": title, "format": "json"}
        r = requests.get(base, params=params, timeout=10); r.raise_for_status()
        pages = r.json().get("query", {}).get("pages", {})
        for _, v in pages.items():
            extract = (v.get("extract", "") or "").strip()
            url = f"https://{host}/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
            return extract[:900], url
        return "", f"https://{host}/wiki/" + urllib.parse.quote(title.replace(" ", "_"))

    def _mw_base(self):
        scheme = "https" if self.mw_https else "http"
        host = self.mw_host or "www.example.com"
        return f"{scheme}://{host}/{self.mw_path_prefix}api.php", host, scheme
    def _search_mediawiki(self, query: str, limit: int = 3):
        base, host, scheme = self._mw_base()
        params = {"action": "query", "list": "search", "srsearch": query, "srlimit": limit, "format": "json"}
        r = requests.get(base, params=params, timeout=10); r.raise_for_status()
        data = r.json().get("query", {}).get("search", [])
        out = []
        for it in data:
            title = it.get("title")
            url = f"{scheme}://{host}/{self.mw_path_prefix}index.php/" + urllib.parse.quote(title.replace(" ", "_"))
            out.append({"title": title, "url": url, "snippet": it.get("snippet", "")})
        return out
    def _mediawiki_summary(self, title: str) -> Tuple[str, str]:
        base, host, scheme = self._mw_base()
        params = {"action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1, "titles": title, "format": "json"}
        r = requests.get(base, params=params, timeout=10); r.raise_for_status()
        pages = r.json().get("query", {}).get("pages", {})
        url = f"{scheme}://{host}/{self.mw_path_prefix}index.php/" + urllib.parse.quote(title.replace(" ", "_"))
        for _, v in pages.items():
            extract = (v.get("extract", "") or "").strip()
            return extract[:900], url
        return "", url

@dataclass
class KBLookupTool(FunctionTool):
    name: str = "kb_lookup"
    description: str = "在内容包/KB中查找预置答案（含图片列表）。"
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "关键词"}
        },
        "required": ["keyword"]
    })
    star_ref: "GameGuide" | None = None

    async def call(self, event: AstrMessageEvent, keyword: str) -> dict:
        star = self.star_ref
        if not star:
            return {"found": False}
        entry = star.pack.match(keyword)
        if entry:
            texts = [blk["content"] for blk in entry.blocks if blk.get("type")=="text"]
            images = []
            for blk in entry.blocks:
                if blk.get("type")=="image":
                    images.append(blk.get("src",""))
            return {"found": True, "text": "\n\n".join(texts), "images": images}
        doc = star.kb.lookup(keyword)
        if doc:
            imgs = [doc.get("image"," ") .strip()] if doc.get("image") else []
            return {"found": True, "text": doc.get("answer",""), "images": imgs}
        return {"found": False}

@dataclass
class WikiSearchTool(FunctionTool):
    name: str = "wiki_search"
    description: str = "Wiki 搜索并返回摘要。"
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "title": {"type": "string", "description": "已知标题（可选）"},
            "limit": {"type": "int", "description": "返回条数（默认3）"}
        },
        "required": ["query"]
    })
    star_ref: "GameGuide" | None = None

    async def call(self, event: AstrMessageEvent, query: str, title: str = "", limit: int = 3) -> dict:
        star = self.star_ref
        if not star:
            return {"ok": False, "error": "star not ready"}
        wiki = star.wiki
        try:
            if title:
                summary, url = wiki.page_summary(title)
                return {"ok": True, "results": [{"title": title, "summary": summary, "url": url}]}
            items = wiki.search(query, limit=limit or 3)
            if not items:
                return {"ok": True, "results": []}
            first = items[0]
            summary, url = wiki.page_summary(first["title"]
            )
            first["summary"] = summary
            first["url"] = url
            return {"ok": True, "results": [first] + items[1:]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

@register("astrbot_plugin_df", "your_name", "DF 攻略（内容包+KB+Wiki+LLM）", "1.2.1")
class GameGuide(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or AstrBotConfig({})

        pack_dir = self.config.get("pack_dir", _abspath("packs", "df"))
        self.pack = ContentPack(pack_dir)

        kb_path = _abspath("kb", "keywords.json")
        self.kb = KeywordKB(kb_path)

        self.wiki = WikiClient(
            mode=self.config.get("wiki_mode","mediawiki"),
            lang=self.config.get("wiki_lang","en"),
            fandom_site=self.config.get("fandom_site",""),
            mw_host=self.config.get("mw_host","dwarffortresswiki.org"),
            mw_https=bool(self.config.get("mw_https", True)),
            mw_path_prefix=self.config.get("mw_path_prefix",""
            )
        )

        self.kb_tool = KBLookupTool(star_ref=self)
        self.wiki_tool = WikiSearchTool(star_ref=self)
        try:
            self.context.add_llm_tools(self.kb_tool, self.wiki_tool)
        except Exception:
            try:
                mgr = self.context.provider_manager.llm_tools
                mgr.func_list.extend([self.kb_tool, self.wiki_tool])
            except Exception as e2:
                logger.warn(f"LLM tools registration failed: {e2}")

    @filter.command("df", alias=["攻略", "game", "guide"])
    async def cmd_df(self, event: AstrMessageEvent):
        q = event.message_str.strip().split(maxsplit=1)
        if len(q) < 2 or not q[1].strip():
            yield event.plain_result("用法：/df <关键词>  （内容包→KB→Wiki）")
            return
        keyword = q[1].strip()

        entry = self.pack.match(keyword)
        if entry:
            chain = self.pack.render_chain(entry)
            yield event.chain_result(chain if chain else [Comp.Plain("该条目没有内容。")])
            return

        doc = self.kb.lookup(keyword)
        if doc:
            chain = []
            ans = (doc.get("answer") or "").strip()
            if ans:
                chain.append(Comp.Plain(ans))
            img = (doc.get("image") or "").strip()
            if img:
                if img.startswith("http"):
                    chain.append(Comp.Image.fromURL(img))
                else:
                    local = _abspath("assets", "images", img) if not os.path.isabs(img) else img
                    if os.path.exists(local):
                        chain.append(Comp.Image.fromFileSystem(local))
            yield event.chain_result(chain) if chain else event.plain_result("KB 命中但无内容。")
            return

        try:
            items = self.wiki.search(keyword, limit=int(self.config.get("wiki_limit", 3)))
            if not items:
                yield event.plain_result("未找到相关条目。")
                return
            title = items[0]["title"]
            summary, url = self.wiki.page_summary(title)
            yield event.plain_result(f"{title}\n{summary[:900]}\n{url}")
        except Exception as e:
            yield event.plain_result(f"Wiki 搜索失败：{e}")

    @filter.command("df.wiki")
    async def cmd_wiki(self, event: AstrMessageEvent):
        q = event.message_str.strip().split(maxsplit=1)
        if len(q) < 2:
            yield event.plain_result("用法：/df.wiki <关键词或标题>")
            return
        keyword = q[1].strip()
        try:
            items = self.wiki.search(keyword, limit=int(self.config.get("wiki_limit", 5)))
            if not items:
                yield event.plain_result("没有搜索到相关条目。")
                return
            title = items[0]["title"]
            summary, url = self.wiki.page_summary(title)
            yield event.plain_result(f"{title}\n{summary[:900]}\n{url}")
        except Exception as e:
            yield event.plain_result(f"Wiki 调用失败：{e}")

    @filter.command("df.kb")
    async def cmd_kb(self, event: AstrMessageEvent):
        q = event.message_str.strip().split(maxsplit=1)
        if len(q) < 2:
            yield event.plain_result("用法：/df.kb <关键词>")
            return
        keyword = q[1].strip()
        entry = self.pack.match(keyword)
        if entry:
            chain = self.pack.render_chain(entry)
            yield event.chain_result(chain) if chain else event.plain_result("条目没有内容。")
            return
        doc = self.kb.lookup(keyword)
        if doc:
            chain = []
            ans = (doc.get("answer") or "").strip()
            if ans:
                chain.append(Comp.Plain(ans))
            img = (doc.get("image") or "").strip()
            if img:
                if img.startswith("http"):
                    chain.append(Comp.Image.fromURL(img))
                else:
                    local = _abspath("assets", "images", img) if not os.path.isabs(img) else img
                    if os.path.exists(local):
                        chain.append(Comp.Image.fromFileSystem(local))
            yield event.chain_result(chain) if chain else event.plain_result("KB 命中但无内容。")
            return
        yield event.plain_result("未命中内容包/KB。")

    @filter.command("dfcfg")
    async def cmd_cfg(self, event: AstrMessageEvent):
        cfg = self.context.get_config()
        pack_dir = cfg.get("pack_dir", _abspath("packs","df"))
        mode = cfg.get("wiki_mode","mediawiki")
        lang = cfg.get("wiki_lang","en")
        mw_host = cfg.get("mw_host","dwarffortresswiki.org")
        mw_path = cfg.get("mw_path_prefix",""
        )
        kb_entries = len(self.kb.entries)
        pack_entries = len(self.pack.by_key)
        yield event.plain_result(f"pack_dir={pack_dir}\nwiki={mode}/{lang}/{mw_host}/{mw_path or '-'}\npack={pack_entries} 条, kb={kb_entries} 条")

    @filter.command("df.sync")
    async def cmd_sync(self, event: AstrMessageEvent):
        try:
            self.pack.reload()
            yield event.plain_result(f"内容包已重载：{len(self.pack.by_key)} 条。")
        except Exception as e:
            yield event.plain_result(f"重载失败：{e}")

    @filter.command("df.list")
    async def cmd_list(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split(maxsplit=1)
        prefix = parts[1].strip().lower() if len(parts) > 1 else ""
        keys = sorted({e.key for e in self.pack.by_key.values()})
        if prefix:
            keys = [k for k in keys if k.lower().startswith(prefix)]
        yield event.plain_result("（空）" if not keys else "\n".join(keys))

    @filter.command("df.ping")
    async def df_ping(self, event: AstrMessageEvent):
        yield event.plain_result("df pong")

    @filter.command("df.where")
    async def df_where(self, event: AstrMessageEvent):
        cfg = self.context.get_config()
        pack_dir = cfg.get("pack_dir", _abspath("packs","df"))
        count = len(self.pack.by_key) if hasattr(self, "pack") else -1
        yield event.plain_result(f"__file__={__file__}\npack_dir={pack_dir}\nentries={count}")

    async def terminate(self):
        try:
            cfg = self.context.get_config()
            if hasattr(cfg, "save_config"):
                cfg.save_config()
        except Exception:
            pass
