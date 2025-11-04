# -*- coding: utf-8 -*-
"""通用工具：路径、JSON、归一化、目录准备"""
import os, json, re

PLUGIN_DIR = os.path.dirname(__file__)

def abspath(*p: str) -> str:
    return os.path.join(os.path.dirname(PLUGIN_DIR), *p)  # 上一级是插件根

def read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def ensure_dirs():
    for d in ("catalog", "texts", "images", "assets"):
        os.makedirs(abspath(d), exist_ok=True)

def norm(s: str) -> str:
    s = s.replace("\u3000", " ")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s
