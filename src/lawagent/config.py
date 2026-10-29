"""配置读取。"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_json(rel: str, default):
    p = ROOT / rel
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_config() -> dict:
    return load_json("configs/config.json", {})


def load_synonyms() -> dict:
    return load_json("configs/synonyms.json", {})
