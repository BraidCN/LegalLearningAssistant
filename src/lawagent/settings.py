"""本地版运行设置：模型走本地 Ollama 还是云端 API。

存放在 data/settings.json（只在本机，不随包分发）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .config import ROOT

PATH = ROOT / "data" / "settings.json"


def load() -> dict:
    try:
        if PATH.exists():
            return json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save(data: dict) -> dict:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    keep = {k: v for k, v in data.items() if v not in (None, "")}
    PATH.write_text(json.dumps(keep, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(PATH, 0o600)
    except Exception:
        pass
    return keep


def public() -> dict:
    """给前端看的设置（不回传密钥明文）。"""
    s = load()
    return {
        "mode": s.get("mode") or "local",
        "base_url": s.get("base_url") or "",
        "cloud_model": s.get("model") or "",
        "local_model": s.get("local_model") or "",
        "has_key": bool(s.get("api_key")),
    }


STORAGE_KEYS = ("docs_dir", "users_dir", "wiki_raw")


def storage() -> dict:
    """归档存储位置（本机设置，空字符串表示用 config.json 的默认值）。"""
    s = load().get("storage") or {}
    return {k: str(s.get(k) or "") for k in STORAGE_KEYS}


def save_storage(data: dict) -> dict:
    s = load()
    cur = s.setdefault("storage", {})
    for k in STORAGE_KEYS:
        v = str(data.get(k) or "").strip()
        if v:
            cur[k] = v
        else:
            cur.pop(k, None)
    if not cur:
        s.pop("storage", None)
    save(s)
    return storage()


def apply_storage(cfg: dict) -> dict:
    st = storage()
    ing = cfg.setdefault("ingest", {})
    for k in STORAGE_KEYS:
        if st.get(k):
            ing[k] = st[k]
    return cfg


def apply(cfg: dict) -> dict:
    s = load()
    mode = (s.get("mode") or "local").lower()
    llm = cfg.setdefault("llm", {})
    routing = cfg.setdefault("routing", {})
    if mode == "cloud":
        llm["cloud_enabled"] = True
        routing["force"] = "cloud"
    else:
        llm["cloud_enabled"] = False
        routing["force"] = "local"
    if s.get("api_key"):
        llm.setdefault("cloud", {})["api_key"] = s["api_key"]
    if s.get("base_url"):
        llm.setdefault("cloud", {})["base_url"] = s["base_url"]
    if s.get("model"):
        llm.setdefault("cloud", {})["model"] = s["model"]
    if s.get("local_model"):
        llm["model"] = s["local_model"]
    apply_storage(cfg)
    return cfg
