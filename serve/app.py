"""LawAgent 本地服务（标准库 http.server，无框架依赖）。

启动：python serve/app.py  （或双击 启动.cmd）

端点：
    GET  /                单页界面
    GET  /api/health      健康检查（含 learn / agent 模块可用性）
    POST /api/ask         问答        -> pipeline.ask
    POST /api/explain     概念解释    -> learn.explain
    POST /api/case        案例分析    -> learn.analyze_case
    POST /api/quiz        生成自测题  -> learn.quiz_create
    POST /api/grade       单题批改    -> learn.quiz_grade
    GET  /api/wrongbook   错题本      -> learn.wrongbook
    POST /api/upload      上传文件    -> extract + docs.add_document（返回 doc_id）
    POST /api/agent/ingest   归档     -> agent.run（决策 + 执行 + 核验）
    POST /api/agent/confirm  确认去向 -> agent.confirm（记住规则并重跑）
    GET  /api/receipts    归档回执列表
    GET  /api/docs        资料库列表
    POST /api/docs/search 资料库检索

learn.py / agent.py 由并行开发提供：缺失或导入失败时，相关端点返回 503 与中文提示，
服务本身照常启动（问答不受影响）。
"""
import importlib
import json
import os
import re
import subprocess
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lawagent import auth, pipeline, settings  # noqa: E402
from lawagent.config import load_config  # noqa: E402

CONFIG = settings.apply(load_config())
STATIC = Path(__file__).resolve().parent / "static"

MAX_BODY = 64 * 1024
MAX_UPLOAD = int(CONFIG.get("ingest", {}).get("max_upload_mb", 50)) * 1024 * 1024
LEARN_MISSING = "学习模块（learn.py）尚未就绪：概念 / 案例 / 自测 / 错题本暂不可用，问答功能不受影响。"
AGENT_MISSING = "归档模块（agent.py）尚未就绪：上传 / 归档暂不可用。"
QUIZ_MAX = 20

_LEARN = None
_LEARN_ERR = ""
_AGENT = None
_AGENT_ERR = ""


def _lazy(name):
    """按需导入子模块；失败返回 (None, 错误说明)。不缓存失败，便于热更新。"""
    try:
        importlib.invalidate_caches()
        mod = importlib.import_module("lawagent." + name)
        return mod, ""
    except Exception as exc:  # noqa: BLE001
        return None, "%s: %s" % (type(exc).__name__, exc)


def get_learn():
    """按需导入 learn；失败返回 None。"""
    global _LEARN, _LEARN_ERR
    if _LEARN is not None:
        return _LEARN
    _LEARN, _LEARN_ERR = _lazy("learn")
    return _LEARN


def get_agent():
    """按需导入 agent；失败返回 None。"""
    global _AGENT, _AGENT_ERR
    if _AGENT is not None:
        return _AGENT
    _AGENT, _AGENT_ERR = _lazy("agent")
    return _AGENT


def _elapsed(t0):
    return int((time.time() - t0) * 1000)


def _as_list(value):
    return value if isinstance(value, list) else []


def _pick(data, *keys):
    """取第一个非空字符串字段（去首尾空白）。"""
    for key in keys:
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def call_learn(fn, *args, **kwargs):
    """调用 learn 里的函数；统一注入 cfg，兼容尚未支持 cfg 关键字的实现。"""
    kwargs.setdefault("cfg", CONFIG)
    try:
        return fn(*args, **kwargs)
    except TypeError as exc:
        if "cfg" not in str(exc):
            raise
        kwargs.pop("cfg", None)
        return fn(*args, **kwargs)


def _shape(res, endpoint, elapsed, extra=None):
    """把 learn 的返回值整理成前端统一的载荷。

    契约：answer / citations / gate 必有；route / retrieved / web_sources / llm
    缺失时补默认值，保证界面组件复用同一套渲染逻辑。
    """
    if not isinstance(res, dict):
        res = {"answer": "" if res is None else str(res)}
    gate = res.get("gate") if isinstance(res.get("gate"), dict) else {}
    citations = res.get("citations")
    if not isinstance(citations, list):
        citations = _as_list(gate.get("citations"))
    gate_cites = _as_list(gate.get("citations")) or list(citations)

    out = dict(res)
    out["answer"] = "" if res.get("answer") is None else str(res.get("answer"))
    out["citations"] = citations
    out["gate"] = {
        "pass": bool(gate.get("pass", True)),
        "citations": gate_cites,
        "errors": [str(e) for e in _as_list(gate.get("errors"))],
    }
    if not isinstance(out.get("route"), dict):
        out["route"] = {}
    for key in ("retrieved", "web_sources"):
        if not isinstance(out.get(key), list):
            out[key] = []
    if not isinstance(out.get("llm"), dict):
        out["llm"] = {"used": False}
    out["endpoint"] = endpoint
    if not out.get("elapsed_ms"):
        out["elapsed_ms"] = elapsed
    if extra:
        out.update(extra)
    return out


def _quiz_option(opt, i):
    """选项统一成 {"label": "A", "text": "选项正文"}；兼容字符串与 dict 两种写法。"""
    label = "ABCDEFGH"[i] if i < 8 else str(i + 1)
    if isinstance(opt, dict):
        text = opt.get("text")
        if text is None:
            text = opt.get("content")
        if text is None:
            text = opt.get("label")
        return {"label": str(opt.get("label") or label), "text": "" if text is None else str(text)}
    s = str(opt).strip()
    m = re.match(r"^([A-Za-z])\s*[.、。:：)）]\s*", s)
    if m:
        return {"label": m.group(1).upper(), "text": s[m.end():].strip()}
    return {"label": label, "text": s}


def _shape_quiz(res, elapsed, topic, n):
    """出题结果归一：learn 用 items（每题含 id/options/answer/article_ref/gate）。"""
    out = _shape(res, "quiz", elapsed, {"topic": topic, "requested": n})
    raw_items = _as_list(res.get("questions")) or _as_list(res.get("items"))
    questions = []
    for i, item in enumerate(raw_items, 1):
        if not isinstance(item, dict):
            continue
        q = dict(item)
        qid = q.get("id") or q.get("item_id") or "q%d" % i
        q["id"] = str(qid)
        q["item_id"] = q["id"]
        q["question"] = str(q.get("question") or q.get("stem") or q.get("q") or "")
        options = [_quiz_option(o, j) for j, o in enumerate(_as_list(q.get("options")))]
        q["options"] = options
        q["option_texts"] = [o["text"] for o in options]
        if not q.get("answer"):
            q["answer"] = q.get("correct_answer", "")
        questions.append(q)
    out["questions"] = questions
    out["items"] = questions
    out["count"] = len(questions)
    return out


class BadRequest(Exception):
    """请求不合法，按 400 返回。"""


class Handler(BaseHTTPRequestHandler):
    server_version = "LawAgent/0.4"

    # ---------- 基础输出 ----------

    def _send(self, body: bytes, content_type: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", code)

    def _error(self, message: str, code: int = 500, **extra):
        payload = {"error": message, "code": code}
        payload.update(extra)
        self._json(payload, code)

    def _internal_error(self):
        """兜底：日志留堆栈，响应只给中文提示，不暴露细节。"""
        traceback.print_exc(file=sys.stderr)
        self._error("服务器内部错误，请稍后重试（详情见服务端日志）。", 500)

    def _read_json(self) -> dict:
        """读取请求体：严格按 UTF-8 解码，非法 JSON 返回 400。"""
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            raise BadRequest("请求头 Content-Length 不合法。")
        if length < 0 or length > MAX_BODY:
            raise BadRequest("请求体过大（上限 %d KB）。" % (MAX_BODY // 1024))
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise BadRequest("请求体不是合法的 UTF-8 文本。")
        text = text.lstrip("\ufeff")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise BadRequest("请求体不是合法 JSON。")
        if not isinstance(data, dict):
            raise BadRequest("请求体必须是 JSON 对象。")
        return data

    def _read_multipart(self):
        """手写 multipart/form-data 解析（标准库无现成实现）。返回 (fields, files)。"""
        ctype = self.headers.get("Content-Type", "") or ""
        if "multipart/form-data" not in ctype.lower():
            raise BadRequest("上传请使用 multipart/form-data。")
        m = re.search(r"boundary=([^;]+)", ctype)
        if not m:
            raise BadRequest("上传请求缺少 boundary。")
        boundary = m.group(1).strip().strip('"').encode("utf-8")
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            raise BadRequest("上传请求 Content-Length 不合法。")
        if length <= 0:
            raise BadRequest("上传内容为空。")
        if length > MAX_UPLOAD:
            raise BadRequest("文件过大（上限 %d MB）。" % (MAX_UPLOAD // 1024 // 1024))
        body = self.rfile.read(length)
        fields, files = {}, []
        for part in body.split(b"--" + boundary):
            if not part or part in (b"--", b"--\r\n", b"\r\n"):
                continue
            if part.startswith(b"\r\n"):
                part = part[2:]
            if part.endswith(b"\r\n"):
                part = part[:-2]
            head, sep, data = part.partition(b"\r\n\r\n")
            if not sep:
                continue
            info = {}
            for line in head.decode("utf-8", "replace").split("\r\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    info[k.strip().lower()] = v.strip()
            disp = info.get("content-disposition", "")
            name_m = re.search(r'name="([^"]*)"', disp)
            file_m = re.search(r'filename="([^"]*)"', disp)
            if file_m:
                files.append({"field": name_m.group(1) if name_m else "file",
                              "filename": file_m.group(1), "data": data})
            elif name_m:
                fields[name_m.group(1)] = data.decode("utf-8", "replace")
        return fields, files

    def _learn_or_503(self):
        learn = get_learn()
        if learn is None:
            self._json({
                "error": LEARN_MISSING,
                "unavailable": True,
                "module": "learn",
                "detail": _LEARN_ERR,
            }, 503)
            return None
        return learn

    def _agent_or_503(self):
        agent = get_agent()
        if agent is None:
            self._json({
                "error": AGENT_MISSING,
                "unavailable": True,
                "module": "agent",
                "detail": _AGENT_ERR,
            }, 503)
            return None
        return agent

    # ---------- 鉴权 ----------

    PUBLIC_GET = {"/", "/index.html", "/api/health"}
    PUBLIC_POST = {"/api/auth/register", "/api/auth/login"}

    def _auth_user(self):
        """个人本地版：固定单用户，无令牌、无登录。"""
        return {"user_id": "owner", "name": "本地用户", "role": "admin", "disabled": 0}, 0

    def _gate(self, path: str, method: str) -> bool:
        public = self.PUBLIC_GET if method == "GET" else self.PUBLIC_POST
        if path in public or not path.startswith("/api/"):
            return True
        self.user, code = self._auth_user()
        if self.user is None:
            if code == 403:
                self._error("forbidden", 403)
            else:
                self._error("unauthorized", 401)
            return False
        return True

    def _require_admin(self) -> bool:
        if (self.user or {}).get("role") != "admin":
            self._error("forbidden", 403)
            return False
        return True

    def _uid(self) -> str:
        return self.user["user_id"]

    # ---------- 路由 ----------

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self.user = None
            parsed = urlsplit(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query or "")
            if not self._gate(path, "GET"):
                return
            if path in ("/", "/index.html"):
                self._serve_index()
                return
            if path == "/api/health":
                self._api_health()
                return
            if path == "/api/settings":
                self._api_settings_get()
                return
            if path == "/api/storage":
                self._api_storage_get()
                return
            if path == "/api/auth/me":
                self._api_auth_me()
                return
            if path == "/api/wrongbook":
                self._api_wrongbook()
                return
            if path == "/api/receipts":
                self._api_receipts(query)
                return
            if path == "/api/docs":
                self._api_docs(query)
                return
            self._error("接口不存在：%s" % path, 404)
        except BadRequest as exc:
            self._error(str(exc), 400)
        except auth.AuthError as exc:
            self._error(str(exc), exc.status)
        except Exception:  # noqa: BLE001
            self._internal_error()

    def do_POST(self):
        try:
            self.user = None
            path = urlsplit(self.path).path
            path = path.rstrip("/") or "/"
            if not self._gate(path, "POST"):
                return
            if path == "/api/settings":
                self._api_settings_set()
                return
            if path == "/api/storage":
                self._api_storage_set()
                return
            if path == "/api/pick-folder":
                self._api_pick_folder()
                return
            if path == "/api/auth/register":
                self._api_auth_register()
                return
            if path == "/api/auth/login":
                self._api_auth_login()
                return
            if path == "/api/auth/rotate":
                self._api_auth_rotate()
                return
            routes = {
                "/api/ask": self._api_ask,
                "/api/explain": self._api_explain,
                "/api/case": self._api_case,
                "/api/quiz": self._api_quiz,
                "/api/grade": self._api_grade,
                "/api/upload": self._api_upload,
                "/api/agent/ingest": self._api_agent_ingest,
                "/api/agent/confirm": self._api_agent_confirm,
                "/api/docs/search": self._api_docs_search,
            }
            handler = routes.get(path)
            if handler is None:
                self._error("接口不存在：%s" % path, 404)
                return
            handler()
        except BadRequest as exc:
            self._error(str(exc), 400)
        except auth.AuthError as exc:
            self._error(str(exc), exc.status)
        except Exception:  # noqa: BLE001
            self._internal_error()

    def do_PATCH(self):
        try:
            self.user = None
            path = urlsplit(self.path).path
            path = path.rstrip("/") or "/"
            if not self._gate(path, "PATCH"):
                return
            m = None
            if m:
                if self._require_admin():
                    self._api_admin_invite_patch(unquote(m.group(1)))
                return
            self._error("接口不存在：%s" % path, 404)
        except BadRequest as exc:
            self._error(str(exc), 400)
        except auth.AuthError as exc:
            self._error(str(exc), exc.status)
        except Exception:  # noqa: BLE001
            self._internal_error()

    # ---------- GET ----------

    def _serve_index(self):
        p = STATIC / "index.html"
        if not p.exists():
            self._error("index.html 缺失", 500)
            return
        self._send(p.read_bytes(), "text/html; charset=utf-8")

    def _serve_admin(self):
        p = STATIC / "admin.html"
        if not p.exists():
            self._error("admin.html 缺失", 500)
            return
        self._send(p.read_bytes(), "text/html; charset=utf-8")

    def _api_health(self):
        learn = get_learn()
        agent = get_agent()
        self._json({
            "ok": True,
            "llm": CONFIG.get("llm", {}).get("backend"),
            "learn": learn is not None,
            "learn_error": "" if learn is not None else _LEARN_ERR,
            "agent": agent is not None,
            "agent_error": "" if agent is not None else _AGENT_ERR,
            "endpoints": [
                "/api/auth/register", "/api/auth/me", "/api/auth/rotate",
                "/api/ask", "/api/explain", "/api/case",
                "/api/quiz", "/api/grade", "/api/wrongbook",
                "/api/upload", "/api/agent/ingest", "/api/agent/confirm",
                "/api/receipts", "/api/docs", "/api/docs/search",
            ],
        })

    # ---------- 认证 / 管理 ----------

    def _api_auth_register(self):
        data = self._read_json()
        res = auth.register(data.get("code") or "", data.get("name") or "", CONFIG)
        self._json(res)

    def _api_settings_get(self):
        self._json(settings.public())

    def _api_settings_set(self):
        data = self._read_json()
        payload = {
            "mode": "cloud" if str(data.get("mode", "")).lower() == "cloud" else "local",
            "api_key": (data.get("api_key") or "").strip(),
            "base_url": (data.get("base_url") or "").strip(),
            "model": (data.get("cloud_model") or "").strip(),
            "local_model": (data.get("local_model") or "").strip(),
        }
        if payload["mode"] == "cloud" and not payload["api_key"] and not settings.load().get("api_key"):
            self._error("切到云端前请先填写 API 密钥", 400)
            return
        merged = settings.load()
        for k, v in payload.items():
            if v:
                merged[k] = v
        merged["mode"] = payload["mode"]
        settings.save(merged)
        settings.apply(CONFIG)
        self._json({"ok": True, "settings": settings.public()})

    # ---------- 归档存储位置 ----------

    def _storage_item(self, key, default):
        from lawagent import docs  # noqa: PLC0415
        ing = CONFIG.get("ingest", {})
        raw = settings.storage().get(key) or ing.get(key) or default
        p = Path(raw)
        if not p.is_absolute():
            p = ROOT / p
        item = {"set": settings.storage().get(key) or "", "resolved": str(p),
                "exists": p.exists(), "writable": False}
        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / ".lawagent_write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            item["writable"] = True
        except Exception as exc:  # noqa: BLE001
            item["error"] = str(exc)[:160]
        return item

    def _api_storage_get(self):
        self._json({
            "ok": True,
            "root": str(ROOT),
            "paths": {
                "docs_dir": self._storage_item("docs_dir", "data/docs"),
                "users_dir": self._storage_item("users_dir", "data/users"),
                "wiki_raw": self._storage_item("wiki_raw", "data/wiki_raw"),
            },
        })

    def _api_storage_set(self):
        data = self._read_json()
        payload = {}
        for k in settings.STORAGE_KEYS:
            v = str(data.get(k) or "").strip().strip('"')
            if not v:
                payload[k] = ""
                continue
            v = os.path.expandvars(os.path.expanduser(v))
            p = Path(v)
            if not p.is_absolute():
                p = ROOT / p
            try:
                p.mkdir(parents=True, exist_ok=True)
                probe = p / ".lawagent_write_test"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
            except Exception as exc:  # noqa: BLE001
                self._error("目录不可写：%s（%s）" % (p, str(exc)[:120]), 400)
                return
            payload[k] = str(p)
        settings.save_storage(payload)
        settings.apply_storage(CONFIG)
        self._api_storage_get()

    def _api_pick_folder(self):
        data = self._read_json()
        initial = str(data.get("initial") or "").strip()
        ps = ["Add-Type -AssemblyName System.Windows.Forms;",
              "$d=New-Object System.Windows.Forms.FolderBrowserDialog;",
              "$d.Description='Select archive folder';",
              "$d.ShowNewFolderButton=$true;"]
        if initial:
            ps.append("$d.SelectedPath='%s';" % initial.replace("'", "''"))
        ps.append("if($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK)"
                  "{[Console]::Out.Write($d.SelectedPath)}")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", "".join(ps)],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=300)
        except Exception as exc:  # noqa: BLE001
            self._error("打开文件夹选择框失败：%s" % exc, 500)
            return
        path = (out.stdout or "").strip()
        self._json({"ok": True, "path": path, "cancelled": not bool(path)})

    def _api_auth_login(self):
        data = self._read_json()
        res = auth.login(data.get("name") or "", data.get("pin") or "", CONFIG)
        self._json(res)

    def _api_auth_me(self):
        user = self.user or {}
        self._json({
            "user_id": user.get("user_id"),
            "name": user.get("name"),
            "role": user.get("role"),
        })

    def _api_auth_rotate(self):
        token = auth.rotate_token(self._uid(), CONFIG)
        self._json({"ok": True, "user_id": self._uid(), "token": token})

    def _api_admin_invites(self):
        data = self._read_json()
        invite = auth.create_invite(
            max_uses=data.get("max_uses", 1),
            days=data.get("days", 7),
            note=str(data.get("note") or ""),
            cfg=CONFIG,
        )
        self._json(invite)

    def _api_admin_stats(self):
        from lawagent import docs  # noqa: PLC0415
        payload = auth.stats(CONFIG)
        payload.update(docs.stats(CONFIG))
        self._json(payload)

    def _api_admin_invites_list(self):
        self._json(auth.list_invites(CONFIG))

    def _api_admin_invite_patch(self, code: str):
        data = self._read_json()
        self._json(auth.update_invite(code, data, CONFIG))

    def _api_admin_users(self):
        from lawagent import docs  # noqa: PLC0415
        items = auth.list_users(CONFIG)
        counts = docs.stats_by_user(CONFIG)
        for item in items:
            per_user = counts.get(item["user_id"], {})
            item["docs_count"] = int(per_user.get("docs", 0))
            item["ask_count"] = int(per_user.get("questions", 0))
        self._json({"items": items, "count": len(items)})

    def _api_admin_disable(self, user_id: str):
        if user_id == self._uid():
            self._error("不能禁用当前登录的管理员。", 400)
            return
        data = self._read_json()
        current = auth.get_user(user_id, CONFIG)
        if current is None:
            self._error("用户不存在。", 404)
            return
        if "disabled" in data:
            disabled = bool(data.get("disabled"))
        else:
            disabled = True
        user = auth.set_disabled(user_id, disabled, CONFIG)
        self._json({"ok": True, "user": user})

    def _api_admin_enable(self, user_id: str):
        current = auth.get_user(user_id, CONFIG)
        if current is None:
            self._error("用户不存在。", 404)
            return
        user = auth.set_disabled(user_id, False, CONFIG)
        self._json({"ok": True, "user": user})

    def _api_wrongbook(self):
        learn = self._learn_or_503()
        if learn is None:
            return
        t0 = time.time()
        res = call_learn(learn.wrongbook, user_id=self._uid())
        if isinstance(res, list):
            res = {"items": res}
        payload = _shape(res, "wrongbook", _elapsed(t0))
        payload["items"] = [i for i in _as_list(payload.get("items")) if isinstance(i, dict)]
        payload["count"] = len(payload["items"])
        self._json(payload)

    def _api_receipts(self, query):
        agent = self._agent_or_503()
        if agent is None:
            return
        from lawagent import docs  # noqa: PLC0415
        try:
            limit = int((query.get("limit") or ["20"])[0])
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(limit, 100))
        self._json({"items": docs.list_receipts(limit, CONFIG, self._uid()), "limit": limit})

    def _api_docs(self, query):
        from lawagent import docs  # noqa: PLC0415
        try:
            limit = int((query.get("limit") or ["50"])[0])
        except (TypeError, ValueError):
            limit = 50
        items = docs.list_docs(max(1, min(limit, 200)), CONFIG, self._uid())
        self._json({"items": items, "count": len(items)})

    # ---------- POST ----------

    def _api_ask(self):
        data = self._read_json()
        q = _pick(data, "q", "question")
        if not q:
            self._error("空问题", 400)
            return
        if len(q) > 500:
            q = q[:500]
        try:
            from lawagent import docs  # noqa: PLC0415
            docs.log_ask(self._uid(), CONFIG)
        except Exception:  # noqa: BLE001  统计失败不影响问答
            pass
        t0 = time.time()
        result = pipeline.ask(q, cfg=CONFIG)
        self._json(_shape(result, "ask", _elapsed(t0)))

    def _api_explain(self):
        learn = self._learn_or_503()
        if learn is None:
            return
        data = self._read_json()
        term = _pick(data, "q", "term")
        if not term:
            self._error("请输入要解释的法律概念（参数 q）。", 400)
            return
        term = term[:200]
        t0 = time.time()
        res = call_learn(learn.explain, term)
        self._json(_shape(res, "explain", _elapsed(t0), {"term": term}))

    def _api_case(self):
        learn = self._learn_or_503()
        if learn is None:
            return
        data = self._read_json()
        facts = _pick(data, "q", "facts")
        if not facts:
            self._error("请输入案情描述（参数 q）。", 400)
            return
        facts = facts[:2000]
        t0 = time.time()
        res = call_learn(learn.analyze_case, facts)
        self._json(_shape(res, "case", _elapsed(t0), {"facts": facts}))

    def _api_quiz(self):
        learn = self._learn_or_503()
        if learn is None:
            return
        data = self._read_json()
        topic = _pick(data, "topic", "q")
        if not topic:
            self._error("请输入自测主题（参数 topic）。", 400)
            return
        topic = topic[:100]
        try:
            n = int(data.get("n") or 5)
        except (TypeError, ValueError):
            n = 5
        n = max(1, min(n, QUIZ_MAX))
        t0 = time.time()
        res = call_learn(learn.quiz_create, topic, n, user_id=self._uid())
        self._json(_shape_quiz(res, _elapsed(t0), topic, n))

    def _api_grade(self):
        learn = self._learn_or_503()
        if learn is None:
            return
        data = self._read_json()
        item_id = _pick(data, "item_id", "id")
        answer = _pick(data, "answer", "user_answer", "choice")
        if not item_id:
            self._error("缺少题目编号（参数 item_id）。", 400)
            return
        if not answer:
            self._error("缺少你的作答（参数 answer）。", 400)
            return
        t0 = time.time()
        res = call_learn(learn.quiz_grade, item_id, answer, user_id=self._uid())
        self._json(_shape(res, "grade", _elapsed(t0), {"item_id": item_id, "user_answer": answer}))

    # ---- 归档 ----

    def _api_upload(self):
        agent = self._agent_or_503()
        if agent is None:
            return
        from lawagent import docs, extract  # noqa: PLC0415
        fields, files = self._read_multipart()
        if not files:
            raise BadRequest("没有收到文件。")
        f = files[0]
        name = (f.get("filename") or "file").strip() or "file"
        data = f["data"]
        if not data:
            raise BadRequest("文件内容为空。")
        try:
            info = extract.extract(name, data)
        except extract.Unsupported as exc:
            saved = docs.save_file(name, data, CONFIG, self._uid())
            self._error(str(exc), 400, saved=saved["path"], name=name)
            return
        info["size"] = len(data)
        doc = docs.add_document(info, data, CONFIG, self._uid())
        self._json({
            "ok": True,
            "doc": {k: doc.get(k) for k in ("doc_id", "name", "kind", "title", "chars",
                                            "chunks", "size", "path")},
            "preview": (info.get("text") or "")[:300],
        })

    def _api_agent_ingest(self):
        agent = self._agent_or_503()
        if agent is None:
            return
        data = self._read_json()
        doc_id = _pick(data, "doc_id", "id")
        if not doc_id:
            self._error("缺少 doc_id（先上传文件）。", 400)
            return
        prompt = str(data.get("prompt") or "")[:2000]
        target = _pick(data, "target")
        t0 = time.time()
        res = agent.run(doc_id, prompt=prompt, target=target, cfg=CONFIG, user_id=self._uid())
        if not isinstance(res, dict):
            self._error("归档返回异常。", 500)
            return
        res.setdefault("elapsed_ms", _elapsed(t0))
        self._json(res)

    def _api_agent_confirm(self):
        agent = self._agent_or_503()
        if agent is None:
            return
        data = self._read_json()
        rid = _pick(data, "receipt_id", "id")
        target = _pick(data, "target")
        if not rid:
            self._error("缺少 receipt_id。", 400)
            return
        if not target:
            self._error("缺少归档去向（target）。", 400)
            return
        res = agent.confirm(rid, target, str(data.get("course") or ""), CONFIG, user_id=self._uid())
        self._json(res)

    def _api_docs_search(self):
        from lawagent import docs  # noqa: PLC0415
        data = self._read_json()
        q = _pick(data, "q", "query")
        if not q:
            self._error("请输入检索词（参数 q）。", 400)
            return
        try:
            k = int(data.get("k") or 8)
        except (TypeError, ValueError):
            k = 8
        hits = docs.search(q[:200], max(1, min(k, 30)), CONFIG, self._uid())
        self._json({"q": q, "hits": hits, "count": len(hits)})

    # ---------- 日志 ----------

    def log_message(self, fmt, *args):  # noqa: A003
        sys.stderr.write("[web] " + (fmt % args) + "\n")


def main():
    host = CONFIG.get("app", {}).get("host", "127.0.0.1")
    port = int(os.environ.get("LAWAGENT_PORT") or CONFIG.get("app", {}).get("port", 8787))
    boot = auth.init(CONFIG)
    if boot.get("admin_token"):
        print("首次启动：已创建管理员 owner。")
        print("管理员令牌（仅显示一次，已写入 %s）：" % boot["admin_token_file"])
        print(boot["admin_token"])
    if boot.get("fixed_invite_created"):
        print("已创建固定邀请码 %s（上限 %d 次 / %d 天）。"
              % (auth.FIXED_INVITE_CODE, auth.FIXED_INVITE_MAX_USES, auth.FIXED_INVITE_DAYS))
    for warning in boot.get("warnings") or []:
        sys.stderr.write("[auth] %s\n" % warning)
    print("LawAgent 已启动： http://%s:%d" % (host, port))
    print("模型：", CONFIG.get("llm", {}).get("model"), " 后端：", CONFIG.get("llm", {}).get("backend"))
    print("认证：个人本地版（免登录，仅本机访问）")
    print("学习模块：", "已加载" if get_learn() is not None else "未就绪（概念/案例/自测/错题本返回 503）")
    print("归档模块：", "已加载" if get_agent() is not None else "未就绪（上传/归档返回 503）")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
