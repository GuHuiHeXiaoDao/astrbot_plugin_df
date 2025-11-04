# -*- coding: utf-8 -*-
import os, json, re
_THIS_DIR = os.path.dirname(__file__)
_PLUGIN_ROOT = os.path.dirname(_THIS_DIR)
def abspath(*p: str) -> str: return os.path.join(_PLUGIN_ROOT, *p)
def read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except Exception: return default
def ensure_dirs():
    for d in ("catalog", "texts", "images", "assets"):
        os.makedirs(abspath(d), exist_ok=True)
def norm(s: str) -> str:
    s = s.replace("\u3000", " ")
    s = re.sub(r"\s+", " ", s); return s.strip().lower()
