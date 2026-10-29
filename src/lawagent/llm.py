"""生成模型调用（Ollama 本地 / OpenAI 兼容云端）。"""
import json
import os
import re
import urllib.request
from pathlib import Path

from .config import load_config


def list_ollama(cfg=None):
    cfg = cfg or load_config()
    url = cfg.get("llm", {}).get("ollama_url", "http://127.0.0.1:11434").rstrip("/")
    try:
        with urllib.request.urlopen(url + "/api/tags", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", [])]
    except Exception:
        return []


def _post(url: str, payload: dict, timeout: int, headers=None):
    body = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _load_key(llm: dict) -> str:
    """按 配置 -> 环境变量 -> Hermes .env 的顺序取密钥（不打印）。"""
    cloud = llm.get("cloud", {})
    key = (cloud.get("api_key") or "").strip()
    if key:
        return key
    env_name = cloud.get("api_key_env", "DEEPSEEK_API_KEY")
    key = (os.environ.get(env_name) or "").strip()
    if key:
        return key
    path = (cloud.get("api_key_file") or "")
    path = path.replace("%LOCALAPPDATA%", os.environ.get("LOCALAPPDATA", ""))
    candidates = []
    if path:
        candidates.append(Path(os.path.expanduser(path)))
    candidates.append(Path.home() / "AppData/Local/hermes/.env")
    candidates.append(Path.home() / ".hermes/.env")
    for p in candidates:
        try:
            if not p.exists():
                continue
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                m = re.match(r"\s*" + re.escape(env_name) + r"\s*=\s*(.+)", line)
                if m:
                    return m.group(1).strip().strip('"').strip("'")
        except Exception:
            continue
    return ""


def chat(messages, cfg=None, backend: str = ""):
    """backend: "" 用配置；"local" 走 Ollama；"cloud" 走 OpenAI 兼容云（DeepSeek）。"""
    cfg = cfg or load_config()
    llm = cfg.get("llm", {})
    mode = backend or llm.get("backend", "none")
    timeout = int(llm.get("timeout", 300))

    if mode in ("local", "ollama"):
        url = llm.get("ollama_url", "http://127.0.0.1:11434").rstrip("/")
        payload = {
            "model": llm.get("model", "qwen3.5:9b"),
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": llm.get("temperature", 0.2),
                "num_ctx": llm.get("num_ctx", 8192),
            },
        }
        data = _post(url + "/api/chat", payload, timeout)
        if data.get("done_reason") == "length":
            print("[llm] 生成达到 num_predict=%s 上限，内容可能被截断"
                  % payload["options"]["num_predict"], flush=True)
        return ((data.get("message") or {}).get("content") or "").strip()

    if mode in ("cloud", "openai"):
        cloud = llm.get("cloud", {})
        base = (cloud.get("base_url") or "https://api.deepseek.com/v1").rstrip("/")
        key = _load_key(llm)
        if not key:
            raise RuntimeError("云端密钥未找到（DEEPSEEK_API_KEY）")
        payload = {
            "model": cloud.get("model", "deepseek-flash"),
            "messages": messages,
            "temperature": llm.get("temperature", 0.2),
        }
        data = _post(
            base + "/chat/completions",
            payload,
            int(cloud.get("timeout", 120)),
            {"Authorization": "Bearer " + key},
        )
        return ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()

    return None
