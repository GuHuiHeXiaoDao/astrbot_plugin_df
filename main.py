# -*- coding: utf-8 -*-
import os
from typing import Any, List
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.message_components import Plain, Image
from dfpkg.utils import abspath, ensure_dirs
from dfpkg.catalog import Catalog
from dfpkg.repos import TextRepo, ImageRepo
from dfpkg.resolver import KeywordResolver
@register("astrbot_plugin_df", "df-preset", "DF 预设图文查询（/df 关键词）——无 LLM", "0.4.1")
class GameGuide(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config or {}
        self.fuzzy_threshold = float(self.config.get("fuzzy_threshold", 0.84))
        ensure_dirs()
        self.alias_path = abspath("catalog", "aliases.json")
        self.texts_path = abspath("texts", "texts.json")
        self.images_path = abspath("images", "images.json")
        self.catalog = Catalog(self.alias_path)
        self.texts = TextRepo(self.texts_path)
        self.images = ImageRepo(self.images_path)
        canon_terms = sorted(set(self.texts.entries.keys()) | set(self.images.entries.keys()))
        self.resolver = KeywordResolver(self.catalog.aliases, canon_terms, self.fuzzy_threshold)
        logger.info(f"[GameGuide] loaded. fuzzy={self.fuzzy_threshold} canon={len(canon_terms)} alias={len(self.catalog.aliases)}")
    def _build_chain(self, key: str) -> List[Any]:
        chain: List[Any] = []
        txt = self.texts.get(key); imgs = self.images.get_list(key)
        if txt: chain.append(Plain(txt))
        for src in imgs:
            if src.startswith("http://") or src.startswith("https://"):
                chain.append(Image.fromURL(src))
            else:
                local = src if os.path.isabs(src) else abspath(src)
                if not os.path.exists(local):
                    alt = abspath("assets", src)
                    if os.path.exists(alt): local = alt
                if os.path.exists(local):
                    chain.append(Image.fromFileSystem(local))
                else:
                    chain.append(Plain(f"[提示] 找不到图片：{src}"))
        return chain
    async def _send(self, event: AstrMessageEvent, parts: List[Any]):
        mc = MessageChain(); mc.chain = parts
        await self.context.send_message(event.unified_msg_origin, mc)
    @filter.command("df")
    async def cmd_df(self, event: AstrMessageEvent):
        try:
            parts = event.message_str.strip().split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                await self._send(event, [Plain("用法：/df 关键词")]); return
            raw = parts[1].strip()
            key = self.resolver.resolve(raw)
            if not key:
                await self._send(event, [Plain("没有答案")]); return
            chain = self._build_chain(key)
            if not chain:
                await self._send(event, [Plain("没有答案")])
            else:
                await self._send(event, chain)
        except Exception as e:
            logger.error(f"[GameGuide] /df 失败: {e}")
            await self._send(event, [Plain(f"处理失败：{e}")])
        finally:
            event.stop_event()
    @filter.command("df.sync")
    async def cmd_sync(self, event: AstrMessageEvent):
        try:
            self.catalog.reload(); self.texts.reload(); self.images.reload()
            canon_terms = sorted(set(self.texts.entries.keys()) | set(self.images.entries.keys()))
            self.resolver.update(self.catalog.aliases, canon_terms)
            await self._send(event, [Plain("已重载数据模块。")])
        except Exception as e:
            await self._send(event, [Plain(f"重载失败：{e}")])
        finally:
            event.stop_event()
    @filter.command("df.cat")
    async def cmd_cat(self, event: AstrMessageEvent):
        try:
            items = sorted([f"{a} -> {b}" for a, b in self.catalog.aliases.items()])
            if not items: await self._send(event, [Plain("目录为空")]); return
            text = "已注册别名：\n" + "\n".join(items[:50])
            if len(items) > 50: text += f"\n... 共 {len(items)} 条"
            await self._send(event, [Plain(text)])
        finally:
            event.stop_event()
