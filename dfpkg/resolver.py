# -*- coding: utf-8 -*-
"""关键词解析：精确→别名→前缀/包含→模糊"""
from typing import List, Dict, Optional
import difflib
from .utils import norm

class KeywordResolver:
    def __init__(self, aliases: Dict[str, str], canon_terms: List[str], fuzzy_threshold: float = 0.84):
        self.aliases = aliases            # 已归一化：alias_norm -> target
        self.canon_terms = list(canon_terms)  # 主词列表（大小写保留）
        self.fuzzy_threshold = fuzzy_threshold
        self._canon_norm = [norm(k) for k in self.canon_terms]

    def update(self, aliases: Dict[str, str], canon_terms: List[str]):
        self.aliases = aliases
        self.canon_terms = list(canon_terms)
        self._canon_norm = [norm(k) for k in self.canon_terms]

    def resolve(self, raw: str) -> Optional[str]:
        q = norm(raw)
        if not q: return None

        # 1) exact in canon
        for k, kn in zip(self.canon_terms, self._canon_norm):
            if kn == q:
                return k

        # 2) alias
        if q in self.aliases:
            return self.aliases[q]

        # 3) prefix / contains
        for k, kn in zip(self.canon_terms, self._canon_norm):
            if kn.startswith(q) or q.startswith(kn) or (q in kn) or (kn in q):
                return k

        # 4) fuzzy
        close = difflib.get_close_matches(q, self._canon_norm, n=1, cutoff=self.fuzzy_threshold)
        if close:
            cn = close[0]
            for k, kn in zip(self.canon_terms, self._canon_norm):
                if kn == cn:
                    return k
        return None
