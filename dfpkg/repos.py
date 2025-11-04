# -*- coding: utf-8 -*-
"""文本与图片仓库"""
from typing import Dict, List
from .utils import read_json

class TextRepo:
    def __init__(self, path: str):
        self.path = path
        data = read_json(path, {"entries": {}})
        self.entries: Dict[str, str] = data.get("entries", {})

    def get(self, key: str) -> str:
        return str(self.entries.get(key, "")).strip()

    def reload(self):
        self.__init__(self.path)

class ImageRepo:
    def __init__(self, path: str):
        self.path = path
        data = read_json(path, {"entries": {}})
        raw = data.get("entries", {})
        self.entries: Dict[str, List[str]] = {k: (v if isinstance(v, list) else [v]) for k, v in raw.items()}

    def get_list(self, key: str) -> List[str]:
        return [str(x).strip() for x in self.entries.get(key, []) if str(x).strip()]

    def reload(self):
        self.__init__(self.path)
