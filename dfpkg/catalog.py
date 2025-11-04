# -*- coding: utf-8 -*-
# alias catalog
from typing import Dict
from .utils import ReadJson, Abspath, Normalize
class CatalogRepo:
    def __init__(self, path: str):
        self.path = path
        data = ReadJson(path, {'aliases': {}})
        self.aliases: Dict[str, str] = {Normalize(k): v.strip() for k, v in data.get('aliases', {}).items()}
    def Resolve(self, term: str) -> str:
        return self.aliases.get(Normalize(term), term.strip())
    def Reload(self):
        self.__init__(self.path)
