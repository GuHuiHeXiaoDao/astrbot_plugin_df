# -*- coding: utf-8 -*-
"""
AstrBot 插件：DF 攻略（指令前缀 /df）
关键词检索：使用 KeywordResolver（精确→别名→前缀/包含→模糊）统一解析。
命中“图文”（内容包/KB）→ 只输出图文；否则→ DF Wiki 摘要。
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass
import os, re, json, urllib.parse, difflib

import requests
import yaml

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

# ---------- utils ----------
PLUGIN_DIR = os.path.dirname(__file__)
def _abspath(*p: str) -> str: return os.path.join(PLUGIN_DIR, *p)
def _norm(s: str) -> str: return s.strip().lower()
def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except Exception: return default
def _write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)

def _to_halfwidth(s: str) -> str:
    """全角转半角（常用符号/空格），利于中文检索归一化。"""
    out = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:  # 全角空格
            out.append(' ')
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return ''.join(out)

def _clean_norm(s: str) -> str:
    """更强的标准化：全角→半角，去多余空白，小写。"""
    s = _to_halfwidth(s)
    s = s.replace('\u200b', '')  # 零宽
    s = re.sub(r'\s+', ' ', s)
    return s.strip().lower()

# ---------- content_pack ----------
@dataclass
class ContentEntry:
    key: str; aliases: List[str]; blocks: List[Dict[str, Any]]

class ContentPack:
    def __init__(self, pack_dir: str):
        self.pack_dir = pack_dir
        self.entries_dir = os.path.join(pack_dir, "entries")
        self.assets_dir  = os.path.join(pack_dir, "assets")
        self.by_key: Dict[str, ContentEntry] = {}; self.alias: Dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        self.by_key.clear(); self.alias.clear()
        if not os.path.isdir(self.entries_dir): return
        for fn in os.listdir(self.entries_dir):
            p = os.path.join(self.entries_dir, fn)
            if not os.path.isfile(p): continue
            try:
                if fn.endswith(".md"): e = self._load_md(p)
                elif fn.endswith(".json"): e = self._load_json_entry(p)
                else: continue
                if e:
                    k = _norm(e.key); self.by_key[k] = e; self.alias[k] = k
                    for a in e.aliases: self.alias[_clean_norm(a)] = k
            except Exception as ex:
                logger.error(f"[ContentPack] load fail {p}: {ex}")

    def _split_front_matter(self, text: str):
        if text.startswith("---"):
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
            if m:
                try: front = yaml.safe_load(m.group(1)) or {}
                except Exception: front = {}
                return front, m.group(2)
        return {}, text

    def _parse_md_blocks(self, body: str) -> List[Dict[str, Any]]:
        blocks: List[Dict[str, Any]] = []; pos = 0
        pat = re.compile(r'!\[[^\]]*\]\(([^)]+)\)')
        for m in pat.finditer(body):
            s, e = m.span(); img = m.group(1).strip(); txt = body[pos:s].strip()
            if txt: blocks.append({"type":"text","content":txt})
            blocks.append({"type":"image","src":img}); pos = e
        tail = body[pos:].strip()
        if tail: blocks.append({"type":"text","content":tail})
        return blocks or [{"type":"text","content":body.strip()}]

    def _load_md(self, path: str) -> Optional[ContentEntry]:
        with open(path, "r", encoding="utf-8") as f: text = f.read()
        meta, body = self._split_front_matter(text)
        key = meta.get("key") or os.path.splitext(os.path.basename(path))[0]
        aliases = meta.get("aliases") or []
        blocks = self._parse_md_blocks(body)
        for src in (meta.get("images") or []): blocks.append({"type":"image","src":str(src)})
        return ContentEntry(key, aliases, blocks)

    def _load_json_entry(self, path: str) -> Optional[ContentEntry]:
        data = _read_json(path, {}); key = data.get("key") or os.path.splitext(os.path.basename(path))[0]
        return ContentEntry(key, data.get("aliases") or [], data.get("blocks") or [])

    def match(self, keyword: str) -> Optional[ContentEntry]:
        if not keyword: return None
        k = _norm(keyword); return self.by_key.get(self.alias.get(k,k))

    def render_chain(self, entry: ContentEntry) -> List[Any]:
        chain: List[Any] = []
        for b in entry.blocks:
            if b.get("type")=="text":
                t = str(b.get("content","")).strip()
                if t: chain.append(Comp.Plain(t))
            elif b.get("type")=="image":
                src = str(b.get("src","")).strip()
                if not src: continue
                if src.startswith("http"): chain.append(Comp.Image.fromURL(src))
                else:
                    for cand in (os.path.join(self.entries_dir,src),
                                 os.path.join(self.assets_dir,src),
                                 os.path.join(self.pack_dir,src)):
                        if os.path.exists(cand): chain.append(Comp.Image.fromFileSystem(cand)); break
                    else: chain.append(Comp.Plain(f"[提示] 找不到本地图片：{src}"))
        return chain

# ---------- catalog ----------
class Catalog:
    def __init__(self, path: str):
        self.path = path
        data = _read_json(path, {"aliases": {}, "wiki": {}})
        self.aliases: Dict[str,str] = { _clean_norm(k): v.strip() for k,v in data.get("aliases",{}).items() }
        self.wiki: Dict[str,str] = data.get("wiki",{})
    def resolve(self, term: str) -> str: return self.aliases.get(_clean_norm(term), term.strip())
    def wiki_title(self, term: str) -> Optional[str]:
        return self.wiki.get(term.strip()) or self.wiki.get(self.resolve(term))
    def add_alias(self, alias: str, target: str): self.aliases[_clean_norm(alias)] = target.strip(); self.save()
    def remove_alias(self, alias: str): self.aliases.pop(_clean_norm(alias), None); self.save()
    def save(self): _write_json(self.path, {"aliases": self.aliases, "wiki": self.wiki})

# ---------- kb ----------
class KeywordKB:
    def __init__(self, path: str):
        self.data = _read_json(path, {"aliases": {}, "entries": {}})
        self.alias = { _clean_norm(k): v.strip() for k,v in self.data.get("aliases",{}).items() }
        self.entries = self.data.get("entries",{})
    def normalize(self, k: str) -> str: return self.alias.get(_clean_norm(k), k.strip())
    def lookup(self, k: str) -> Optional[Dict[str, Any]]: return self.entries.get(self.normalize(k))

# ---------- wiki ----------
class DFWikiClient:
    def __init__(self, host: str = "dwarffortresswiki.org", https: bool = True, prefix: str = ""):
        self.host, self.https = host, https
        self.prefix = prefix.strip("/");  self.prefix += ("/" if self.prefix else "")
    def _base(self) -> str: return f"{'https' if self.https else 'http'}://{self.host}/{self.prefix}api.php"
    def _page(self, title: str) -> str: return f"{'https' if self.https else 'http'}://{self.host}/{self.prefix}index.php/" + urllib.parse.quote(title.replace(" ","_"))
    def search(self, q: str, limit: int = 3) -> List[Dict[str,str]]:
        r = requests.get(self._base(), params={"action":"query","list":"search","srsearch":q,"srlimit":limit,"format":"json"}, timeout=10); r.raise_for_status()
        out=[]; 
        for it in r.json().get("query",{}).get("search",[]): out.append({"title": it.get("title"), "url": self._page(it.get("title"))})
        return out
    def summary(self, title: str) -> str:
        r = requests.get(self._base(), params={"action":"query","prop":"extracts","exintro":1,"explaintext":1,"titles":title,"format":"json"}, timeout=10); r.raise_for_status()
        for _,v in r.json().get("query",{}).get("pages",{}).items(): return (v.get("extract","") or "").strip()
        return ""

# ---------- redesigned keyword resolver ----------
class KeywordResolver:
    """
    统一关键字检索：
    - exact：精确命中 key 或别名（pack/kb/catalog）
    - prefix：前缀命中
    - contains：包含命中（原词包含条目名或相反）
    - fuzzy：difflib 相似度（默认阈值 0.84）
    返回（canon, where, matched, score），canon 可用于 pack.match/kb.lookup。
    """
    def __init__(self, pack: ContentPack, kb: KeywordKB, cat: Catalog, fuzzy_threshold: float = 0.84):
        self.pack, self.kb, self.cat = pack, kb, cat
        self.fuzzy_threshold = fuzzy_threshold
        self.index: Dict[str, Tuple[str,str]] = {}  # term_norm -> (where, canonical)
        self._build_index()

    def _add(self, term: str, where: str, canonical: str):
        t = _clean_norm(term)
        if t: self.index[t] = (where, canonical)

    def _build_index(self):
        # pack keys / aliases -> pack:key
        for k in self.pack.by_key.keys():
            self._add(k, "pack", k)
        for alias, key in self.pack.alias.items():
            self._add(alias, "pack", key)
        # kb entries / aliases -> kb:key
        for k in self.kb.entries.keys():
            self._add(k, "kb", k)
        for alias, key in self.kb.alias.items():
            self._add(alias, "kb", key)
        # catalog aliases -> alias:target（where=alias），catalog wiki keys也加入（where=wiki）
        for a, tgt in self.cat.aliases.items():
            self._add(a, "alias", tgt)
        for term in self.cat.wiki.keys():
            self._add(term, "wiki", term)

    def resolve(self, raw: str) -> Tuple[Optional[str], str, str, float]:
        """返回 (canon, where, matched, score)；canon 可为空（未命中）。"""
        q = _clean_norm(raw)
        if not q: return None, "", "", 0.0
        best = (None, "", "", 0.0)  # type: Tuple[Optional[str], str, str, float]

        # 1) exact
        if q in self.index:
            where, canon = self.index[q]
            return canon, where, q, 1.0

        # 预先收集候选
        terms = list(self.index.keys())

        def update(candidate_term: str, score: float):
            nonlocal best
            where, canon = self.index[candidate_term]
            if score > best[3]:
                best = (canon, where, candidate_term, score)

        # 2) prefix / contains
        for t in terms:
            if t.startswith(q):
                update(t, 0.96)
            elif q.startswith(t):
                update(t, 0.93)
            elif t in q or q in t:
                update(t, 0.90)

        # 3) fuzzy
        # 选取与 q 相似的 topN（用 difflib，避免第三方依赖）
        close = difflib.get_close_matches(q, terms, n=5, cutoff=self.fuzzy_threshold)
        for t in close:
            ratio = difflib.SequenceMatcher(None, q, t).ratio()
            update(t, ratio)

        return best  # 可能 (None, "", "", 0.0)

# ---------- query ----------
def chain_from_kb(kb_doc: Dict[str, Any]) -> List[Any]:
    chain: List[Any] = []
    ans = (kb_doc.get("answer") or "").strip()
    img = (kb_doc.get("image") or "").strip()
    if ans: chain.append(Comp.Plain(ans))
    if img:
        if img.startswith("http"): chain.append(Comp.Image.fromURL(img))
        else:
            local = _abspath("assets","images",img) if not os.path.isabs(img) else img
            if os.path.exists(local): chain.append(Comp.Image.fromFileSystem(local))
    return chain

def query_flow(raw: str, pack: ContentPack, kb: KeywordKB, cat: Catalog, wiki: DFWikiClient, resolver: KeywordResolver) -> Dict[str, Any]:
    # 通过 resolver 统一解析 canon 关键词
    canon, where, matched, score = resolver.resolve(raw)
    kw = canon or cat.resolve(raw)

    # 1) 内容包图文
    entry = pack.match(kw)
    if entry:
        return {"type":"chain", "data": pack.render_chain(entry)}

    # 2) KB 图文
    doc = kb.lookup(kw)
    if doc:
        ch = chain_from_kb(doc)
        return {"type":"chain","data": ch} if ch else {"type":"plain","data":"（KB 命中但无图文内容）"}

    # 3) 目录定向 Wiki 标题
    title = cat.wiki_title(raw) or cat.wiki_title(kw) or (kw if where=="wiki" else None)
    if title:
        s = wiki.summary(title)[:900]
        return {"type":"plain","data": f"{title}\n{s}\n{wiki._page(title)}"}

    # 4) DF Wiki 搜索兜底
    items = wiki.search(kw, 3)
    if not items: return {"type":"plain","data":"未在 DF Wiki 找到相关条目。"}
    t0 = items[0]["title"]; s = wiki.summary(t0)[:900]
    return {"type":"plain","data": f"{t0}\n{s}\n{wiki._page(t0)}"}

# ---------- plugin ----------
_STAR: "GameGuide" | None = None

@register("astrbot_plugin_df", "your_name", "DF 攻略（图文优先 + DF Wiki 兜底）", "1.6.0")
class GameGuide(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or AstrBotConfig({})
        self.pack = ContentPack(self.config.get("pack_dir", _abspath("packs","df")))
        self.kb   = KeywordKB(_abspath("kb","keywords.json"))
        self.cat  = Catalog(_abspath("catalog","keywords.json"))
        self.wiki = DFWikiClient(
            host=self.config.get("mw_host","dwarffortresswiki.org"),
            https=bool(self.config.get("mw_https", True)),
            prefix=self.config.get("mw_path_prefix","")
        )
        self.resolver = KeywordResolver(self.pack, self.kb, self.cat, fuzzy_threshold=float(self.config.get("fuzzy_threshold", 0.84)))
        global _STAR; _STAR = self

    @staticmethod
    @filter.command("df", alias=["攻略","game","guide"])
    async def cmd_df(event: AstrMessageEvent):
        star=_STAR
        if star is None: yield event.plain_result("插件尚未初始化完成。"); return
        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts)<2 or not parts[1].strip():
            yield event.plain_result("用法：/df <关键词>（命中图文只发图文，否则 DF Wiki；含模糊匹配）"); return
        raw = parts[1].strip()
        res = query_flow(raw, star.pack, star.kb, star.cat, star.wiki, star.resolver)
        if res["type"]=="chain": yield event.chain_result(res["data"])
        else: yield event.plain_result(res["data"])

    @staticmethod
    @filter.command("df.reg")
    async def cmd_reg(event: AstrMessageEvent):
        star=_STAR
        if star is None: yield event.plain_result("插件尚未初始化完成。"); return
        s = event.message_str.split(" ",1)
        if len(s)<2: yield event.plain_result("用法：/df.reg <别名> -> <目标>"); return
        line = s[1].strip()
        if "->" not in line: yield event.plain_result("格式应为：/df.reg 别名 -> 目标"); return
        alias, target = [x.strip() for x in line.split("->",1)]
        if not alias or not target: yield event.plain_result("别名与目标均不能为空"); return
        star.cat.add_alias(alias, target); star.resolver._build_index()
        yield event.plain_result(f"已注册：{alias} → {target}")

    @staticmethod
    @filter.command("df.rm")
    async def cmd_rm(event: AstrMessageEvent):
        star=_STAR
        if star is None: yield event.plain_result("插件尚未初始化完成。"); return
        s = event.message_str.split(" ",1)
        if len(s)<2 or not s[1].strip(): yield event.plain_result("用法：/df.rm <别名>"); return
        alias = s[1].strip(); star.cat.remove_alias(alias); star.resolver._build_index()
        yield event.plain_result(f"已移除别名：{alias}")

    @staticmethod
    @filter.command("df.cat")
    async def cmd_cat(event: AstrMessageEvent):
        star=_STAR
        if star is None: yield event.plain_result("插件尚未初始化完成。"); return
        if not star.cat.aliases: yield event.plain_result("目录为空"); return
        pairs = [f"{a} -> {b}" for a,b in sorted(star.cat.aliases.items())]
        yield event.plain_result("已注册别名：\n" + "\n".join(pairs))

    @staticmethod
    @filter.command("df.sync")
    async def cmd_sync(event: AstrMessageEvent):
        star=_STAR
        if star is None: yield event.plain_result("插件尚未初始化完成。"); return
        try:
            star.pack.reload()
            star.resolver._build_index()
            yield event.plain_result(f"内容包已重载：{len(star.pack.by_key)} 条。")
        except Exception as e:
            yield event.plain_result(f"重载失败：{e}")

    @staticmethod
    @filter.command("df.list")
    async def cmd_list(event: AstrMessageEvent):
        star=_STAR
        if star is None: yield event.plain_result("插件尚未初始化完成。"); return
        parts = event.message_str.strip().split(maxsplit=1)
        prefix = parts[1].strip().lower() if len(parts)>1 else ""
        keys = sorted({e.key for e in star.pack.by_key.values()})
        if prefix: keys=[k for k in keys if k.lower().startswith(prefix)]
        yield event.plain_result("（空）" if not keys else "\n".join(keys))

    @staticmethod
    @filter.command("dfcfg")
    async def cmd_cfg(event: AstrMessageEvent):
        star=_STAR
        if star is None: yield event.plain_result("插件尚未初始化完成。"); return
        cfg=star.config; pack_dir=cfg.get("pack_dir", _abspath("packs","df"))
        yield event.plain_result(
            f"pack_dir={pack_dir}\n"
            f"wiki={'https' if cfg.get('mw_https', True) else 'http'}://{cfg.get('mw_host','dwarffortresswiki.org')}/{cfg.get('mw_path_prefix','') or '-'}\n"
            f"pack={len(star.pack.by_key)} 条, kb={len(star.kb.entries)} 条, cat={len(star.cat.aliases)} 别名"
        )

    @staticmethod
    @filter.command("df.ping")
    async def df_ping(event: AstrMessageEvent): yield event.plain_result("df pong")
