# -*- coding: utf-8 -*-
# keyword resolver: exact -> alias -> prefix/contains -> fuzzy
from typing import List, Dict, Optional
import difflib
from .utils import Normalize
class KeywordResolver:
    def __init__(self, aliases: Dict[str,str], canon_terms: List[str], fuzzy_threshold: float = 0.84):
        self.aliases = aliases
        self.canon_terms = list(canon_terms)
        self.fuzzy_threshold = fuzzy_threshold
        self._canon_norm = [Normalize(k) for k in self.canon_terms]
    def Update(self, aliases: Dict[str,str], canon_terms: List[str]):
        self.aliases = aliases
        self.canon_terms = list(canon_terms)
        self._canon_norm = [Normalize(k) for k in self.canon_terms]
    def Resolve(self, raw: str) -> Optional[str]:
        q = Normalize(raw)
        if not q: return None
        for k, kn in zip(self.canon_terms, self._canon_norm):
            if kn == q: return k
        if q in self.aliases: return self.aliases[q]
        for k, kn in zip(self.canon_terms, self._canon_norm):
            if kn.startswith(q) or q.startswith(kn) or (q in kn) or (kn in q): return k
        close = difflib.get_close_matches(q, self._canon_norm, n=1, cutoff=self.fuzzy_threshold)
        if close:
            cn = close[0]
            for k, kn in zip(self.canon_terms, self._canon_norm):
                if kn == cn: return k
        return None
