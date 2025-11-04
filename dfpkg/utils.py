# -*- coding: utf-8 -*-
# utils: path/json/normalize/ensure dirs
import os, json, re
_THIS_DIR = os.path.dirname(__file__)
_PLUGIN_ROOT = os.path.dirname(_THIS_DIR)
def Abspath(*p: str) -> str: return os.path.join(_PLUGIN_ROOT, *p)
def ReadJson(path: str, default):
    try:
        with open(path, 'r', encoding='utf-8') as f: return json.load(f)
    except Exception: return default
def EnsureDirs():
    for d in ('catalog','texts','images','assets'):
        os.makedirs(Abspath(d), exist_ok=True)
def Normalize(s: str) -> str:
    s = s.replace('\u3000',' ')
    s = re.sub(r'\s+',' ', s)
    return s.strip().lower()
