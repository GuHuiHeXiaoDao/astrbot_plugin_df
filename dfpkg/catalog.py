# -*- coding: utf-8 -*-
"""别名目录模块"""
from typing import Dict
from .utils import read_json, abspath, norm

class Catalog:
    def __init__(self, path: str):
        self.path = path
        data = read_json(path, {"aliases": {}})
        self.aliases: Dict[str, str] = {norm(k): v.strip() for k, v in data.get("aliases", {}).items()}

    def resolve(self, term: str) -> str:
        return self.aliases.get(norm(term), term.strip())

    def reload(self):
        self.__init__(self.path)
