# -*- coding: utf-8 -*-
# astrbot_plugin_df: local preset text/images, no LLM
from typing import Any, List
import os
import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from dfpkg.utils import Abspath, EnsureDirs
from dfpkg.catalog import CatalogRepo
from dfpkg.repos import TextRepo, ImageRepo
from dfpkg.resolver import KeywordResolver

@register('astrbot_plugin_df', 'DfPreset', 'DF preset answers (/df <term>), no LLM', '0.7.0')
class GameGuide(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config or {}
        self.fuzzyThreshold = float(self.config.get('fuzzy_threshold', 0.84))
        EnsureDirs()
        self.aliasPath = Abspath('catalog', 'aliases.json')
        self.textsPath = Abspath('texts', 'texts.json')
        self.imagesPath = Abspath('images', 'images.json')
        self.catalog = CatalogRepo(self.aliasPath)
        self.texts = TextRepo(self.textsPath)
        self.images = ImageRepo(self.imagesPath)
        canon = sorted(set(self.texts.entries.keys()) | set(self.images.entries.keys()))
        self.resolver = KeywordResolver(self.catalog.aliases, canon, self.fuzzyThreshold)
        logger.info(f'[GameGuide] ready. fuzzy={self.fuzzyThreshold} canon={len(canon)} alias={len(self.catalog.aliases)}')

    def BuildChain(self, key: str) -> List[Any]:
        chain: List[Any] = []
        txt = self.texts.Get(key)
        imgs = self.images.GetList(key)
        if txt:
            chain.append(Comp.Plain(txt))
        for src in imgs:
            if src.startswith('http://') or src.startswith('https://'):
                chain.append(Comp.Image.fromURL(src))
            else:
                local = src if os.path.isabs(src) else Abspath(src)
                if not os.path.exists(local):
                    alt = Abspath('assets', src)
                    if os.path.exists(alt): local = alt
                if os.path.exists(local):
                    chain.append(Comp.Image.fromFileSystem(local))
                else:
                    chain.append(Comp.Plain(f'[提示] 找不到图片：{src}'))
        return chain

    @filter.command('df')
    async def CmdDf(self, event: AstrMessageEvent):
        '''DF query: /df <term>'''
        userName = event.get_sender_name()
        messageStr = event.message_str.strip()
        logger.info(f'[GameGuide] /df by {userName}: {messageStr}')
        parts = messageStr.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            yield event.plain_result('用法：/df 关键词')
            return
        raw = parts[1].strip()
        key = self.resolver.Resolve(raw)
        if not key:
            yield event.plain_result('没有答案')
            return
        chain = self.BuildChain(key)
        if not chain:
            yield event.plain_result('没有答案')
            return
        yield event.chain_result(chain)

    @filter.command('dfsync')
    async def CmdDfSync(self, event: AstrMessageEvent):
        '''Reload data modules'''
        self.catalog.Reload()
        self.texts.Reload()
        self.images.Reload()
        canon = sorted(set(self.texts.entries.keys()) | set(self.images.entries.keys()))
        self.resolver.Update(self.catalog.aliases, canon)
        yield event.plain_result('已重载数据模块。')

    @filter.command('dfcat')
    async def CmdDfCat(self, event: AstrMessageEvent):
        '''Show alias catalog'''
        items = sorted([f"{a} -> {b}" for a, b in self.catalog.aliases.items()])
        if not items:
            yield event.plain_result('目录为空')
            return
        text = '已注册别名：\n' + '\n'.join(items[:50])
        if len(items) > 50:
            text += f'\n... 共 {len(items)} 条'
        yield event.plain_result(text)

    @filter.command('dfping')
    async def CmdDfPing(self, event: AstrMessageEvent):
        '''Health check'''
        yield event.plain_result('df pong')

    async def terminate(self):
        '''cleanup'''
        pass
