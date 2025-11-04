# -*- coding: utf-8 -*-
# texts and images repo
from typing import Dict, List
from .utils import ReadJson
class TextRepo:
    def __init__(self, path: str):
        self.path = path
        data = ReadJson(path, {'entries': {}})
        self.entries: Dict[str, str] = data.get('entries', {})
    def Get(self, key: str) -> str:
        return str(self.entries.get(key, '')).strip()
    def Reload(self):
        self.__init__(self.path)
class ImageRepo:
    def __init__(self, path: str):
        self.path = path
        data = ReadJson(path, {'entries': {}})
        raw = data.get('entries', {})
        self.entries: Dict[str, List[str]] = {k: (v if isinstance(v, list) else [v]) for k, v in raw.items()}
    def GetList(self, key: str) -> List[str]:
        return [str(x).strip() for x in self.entries.get(key, []) if str(x).strip()]
    def Reload(self):
        self.__init__(self.path)
