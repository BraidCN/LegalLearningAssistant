"""多用户认证：邀请码注册、个人令牌、管理员操作。

- 令牌：secrets.token_bytes(32) -> base32 -> 前缀 lk_，只存 sha256
- 邀请码：8 位大写字母数字（去掉易混 0/O/1/I）
- 首次启动（users 为空）创建 owner 管理员，令牌写入 data/db/admin_token.txt
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
from base64 import b32encode
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import ROOT, load_config

DEFAULT_DB = "data/db/auth.db"
DEFAULT_TOKEN_FILE = "data/db/admin_token.txt"
TOKEN_PREFIX = "lk_"
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

# 固定邀请码：仅首次初始化时创建，已存在（含大小写变体）时绝不覆盖
FIXED_INVITE_CODE = "LAWAGENT-LOCAL"
FIXED_INVITE_MAX_USES = 50
FIXED_INVITE_DAYS = 90
FIXED_INVITE_NOTE = "默认邀请码"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  user_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL DEFAULT 'user',
  disabled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT,
  last_seen TEXT,
  invite_code TEXT
);
CREATE TABLE IF NOT EXISTS invite_codes(
  code TEXT PRIMARY KEY,
  max_uses INTEGER NOT NULL DEFAULT 1,
  used INTEGER NOT NULL DEFAULT 0,
  expires_at TEXT,
  note TEXT,
  created_at TEXT,
  disabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS login_fails(
  name TEXT,
  ts REAL
);
"""

PIN_SALT = "law-agent-pin-v1:"


def hash_pin(pin: str) -> str:
    return hashlib.sha256((PIN_SALT + (pin or "")).encode()).hexdigest()


def _ensure_pin_schema(conn) -> None:
    """补 pin_hash 列、昵称唯一索引、登录失败表（幂等）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    if "pin_hash" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN pin_hash TEXT")

    dups = conn.execute(
        "SELECT name FROM users GROUP BY name COLLATE NOCASE HAVING COUNT(*) > 1"
    ).fetchall()
    for d in dups:
        rows = conn.execute(
            "SELECT user_id FROM users WHERE name=? COLLATE NOCASE ORDER BY created_at", (d["name"],)
        ).fetchall()
        for r in rows[1:]:
            conn.execute(
                "UPDATE users SET name=? WHERE user_id=?",
                ("%s-%s" % (d["name"], r["user_id"][-4:]), r["user_id"]),
            )

    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_name ON users(name COLLATE NOCASE)")
    conn.execute("CREATE TABLE IF NOT EXISTS login_fails(name TEXT, ts REAL)")
    conn.commit()


def _gen_unique_pin(conn) -> str:
    for _ in range(500):
        pin = "%04d" % secrets.randbelow(10000)
        if not conn.execute("SELECT 1 FROM users WHERE pin_hash=?", (hash_pin(pin),)).fetchone():
            return pin
    raise AuthError("口令生成失败，请稍后重试。", 500)


class AuthError(Exception):
    """认证失败，带 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _cfg(cfg=None) -> dict:
    return cfg if isinstance(cfg, dict) and cfg else load_config()


def _abs(rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else (ROOT / p)


def db_path(cfg=None) -> Path:
    rel = (_cfg(cfg).get("paths") or {}).get("auth_db", DEFAULT_DB)
    p = _abs(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def admin_token_file(cfg=None) -> Path:
    rel = (_cfg(cfg).get("paths") or {}).get("admin_token_file", DEFAULT_TOKEN_FILE)
    p = _abs(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def connect(cfg=None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path(cfg)), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    if "disabled" not in _table_columns(conn, "invite_codes"):
        conn.execute(
            "ALTER TABLE invite_codes ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()
    _ensure_pin_schema(conn)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso(value: str):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_expired(value: str) -> bool:
    dt = _parse_iso(value)
    return bool(dt and dt <= datetime.now(timezone.utc))


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def new_token() -> str:
    """32 字节随机数 -> base32 -> lk_ 前缀。"""
    raw = b32encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    return TOKEN_PREFIX + raw


def _new_user_id() -> str:
    return "u_" + secrets.token_hex(6)


def _new_code(conn) -> str:
    for _ in range(50):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        if conn.execute("SELECT 1 FROM invite_codes WHERE UPPER(code)=?", (code,)).fetchone() is None:
            return code
    raise AuthError("邀请码生成失败，请重试。", 500)


def _find_invite(conn, code: str):
    """按码查邀请码，大小写不敏感（固定码可小写存储）。"""
    return conn.execute(
        "SELECT * FROM invite_codes WHERE UPPER(code)=? ORDER BY created_at, code LIMIT 1",
        ((code or "").strip().upper(),),
    ).fetchone()


def _invite_public(row) -> dict:
    used = int(row["used"] or 0)
    max_uses = int(row["max_uses"] or 0)
    disabled = bool(row["disabled"])
    expired = _is_expired(row["expires_at"])
    if disabled:
        status = "disabled"
    elif expired:
        status = "expired"
    elif used >= max_uses:
        status = "exhausted"
    else:
        status = "active"
    return {
        "code": row["code"],
        "max_uses": max_uses,
        "used": used,
        "expires_at": row["expires_at"],
        "note": row["note"] or "",
        "created_at": row["created_at"],
        "disabled": disabled,
        "expired": expired,
        "status": status,
    }


def _normalize_expires(value):
    """把管理端输入的日期/ISO 时间转成 UTC ISO；None/空串表示永不过期。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise AuthError("过期时间格式不正确，请使用 ISO 日期（如 2026-12-31）。")
    if len(text) == 10:
        dt = dt.replace(hour=23, minute=59, second=59)
    if dt.tzinfo is None:
        dt = dt.astimezone()  # 视为本机时间
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _ensure_fixed_invite(conn) -> bool:
    """不存在固定邀请码时创建；已存在（任意大小写）则原样保留。"""
    if _find_invite(conn, FIXED_INVITE_CODE) is not None:
        return False
    expires_at = (
        datetime.now(timezone.utc).replace(microsecond=0)
        + timedelta(days=FIXED_INVITE_DAYS)
    ).isoformat()
    try:
        conn.execute(
            "INSERT INTO invite_codes(code, max_uses, used, expires_at, note, created_at, disabled)"
            " VALUES (?,?,?,?,?,?,0)",
            (FIXED_INVITE_CODE, FIXED_INVITE_MAX_USES, 0, expires_at,
             FIXED_INVITE_NOTE, _now()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    return True


def _public_user(row) -> dict:
    return {
        "user_id": row["user_id"],
        "name": row["name"],
        "role": row["role"],
        "disabled": bool(row["disabled"]),
        "created_at": row["created_at"],
        "last_seen": row["last_seen"],
        "invite_code": row["invite_code"],
    }


def _write_admin_token(token: str, cfg=None) -> Path:
    path = admin_token_file(cfg)
    path.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


# ---------------------------------------------------------------- 初始化 / 迁移


def _table_columns(conn, table: str):
    return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()]


def _migrate_articles(cfg) -> str:
    """旧 articles.db 的 quiz_items / wrong_answers 补 user_id。"""
    rel = (_cfg(cfg).get("paths") or {}).get("db", "data/db/articles.db")
    path = _abs(rel)
    if not path.exists():
        return ""
    try:
        conn = sqlite3.connect(str(path), timeout=10)
        try:
            for table in ("quiz_items", "wrong_answers"):
                if not conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone():
                    continue
                if "user_id" not in _table_columns(conn, table):
                    conn.execute(
                        "ALTER TABLE %s ADD COLUMN user_id TEXT NOT NULL DEFAULT 'owner'" % table
                    )
                conn.execute(
                    "UPDATE %s SET user_id='owner' WHERE user_id IS NULL OR user_id=''" % table
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return "articles.db 迁移失败：%s: %s" % (type(exc).__name__, exc)
    return ""


def _migrate_docs(cfg) -> str:
    """旧 docs.db 由 docs.connect 的统一迁移处理，这里只触发一次。"""
    try:
        from . import docs  # noqa: PLC0415

        conn = docs.connect(cfg)
        conn.commit()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        return "docs.db 迁移失败：%s: %s" % (type(exc).__name__, exc)
    return ""


def migrate_legacy(cfg=None) -> list:
    """旧数据 user_id 一律设为 owner（只补空值，不覆盖已有用户）。"""
    cfg = _cfg(cfg)
    warnings = []
    for msg in (_migrate_docs(cfg), _migrate_articles(cfg)):
        if msg:
            warnings.append(msg)
    return warnings


def init(cfg=None) -> dict:
    """确保表存在；users 为空时创建 owner 管理员并输出一次性令牌。"""
    cfg = _cfg(cfg)
    conn = connect(cfg)
    bootstrapped = False
    admin_token = ""
    fixed_invite_created = False
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        if count == 0:
            admin_token = new_token()
            now = _now()
            conn.execute(
                "INSERT INTO users(user_id, name, token_hash, role, disabled,"
                " created_at, last_seen, invite_code) VALUES (?,?,?,?,?,?,?,?)",
                ("owner", "owner", hash_token(admin_token), "admin", 0, now, now, ""),
            )
            conn.commit()
            _write_admin_token(admin_token, cfg)
            bootstrapped = True
        fixed_invite_created = _ensure_fixed_invite(conn)
    finally:
        conn.close()

    warnings = migrate_legacy(cfg)
    return {
        "bootstrapped": bootstrapped,
        "admin_token": admin_token,
        "admin_token_file": str(admin_token_file(cfg)),
        "auth_db": str(db_path(cfg)),
        "fixed_invite_created": fixed_invite_created,
        "warnings": warnings,
    }


# ---------------------------------------------------------------- 用户 / 令牌


def authenticate(token: str, cfg=None):
    """按令牌查用户；无效返回 None，禁用用户也会返回（由调用方决定 403）。"""
    if not token:
        return None
    th = hash_token(token)
    conn = connect(cfg)
    try:
        row = conn.execute("SELECT * FROM users WHERE token_hash=? LIMIT 1", (th,)).fetchone()
        if row is None:
            return None
        if not row["disabled"]:
            conn.execute(
                "UPDATE users SET last_seen=? WHERE user_id=?", (_now(), row["user_id"])
            )
            conn.commit()
        return _public_user(row)
    finally:
        conn.close()


def get_user(user_id: str, cfg=None):
    conn = connect(cfg)
    try:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return _public_user(row) if row else None
    finally:
        conn.close()


def rotate_token(user_id: str, cfg=None) -> str:
    token = new_token()
    conn = connect(cfg)
    try:
        cur = conn.execute(
            "UPDATE users SET token_hash=? WHERE user_id=?", (hash_token(token), user_id)
        )
        conn.commit()
        if cur.rowcount == 0:
            raise AuthError("用户不存在。", 404)
    finally:
        conn.close()
    return token


def register(code: str, name: str, cfg=None) -> dict:
    code = (code or "").strip().upper()
    name = " ".join((name or "").split())
    if not code:
        raise AuthError("请输入邀请码。")
    if not name:
        raise AuthError("请输入昵称。")
    if len(name) > 40:
        name = name[:40]

    conn = connect(cfg)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _find_invite(conn, code)
        if row is None:
            conn.rollback()
            raise AuthError("邀请码无效。")
        if row["disabled"]:
            conn.rollback()
            raise AuthError("邀请码已停用。")
        if _is_expired(row["expires_at"]):
            conn.rollback()
            raise AuthError("邀请码已过期。")
        if int(row["used"]) >= int(row["max_uses"]):
            conn.rollback()
            raise AuthError("邀请码已用完。")

        dup = conn.execute("SELECT 1 FROM users WHERE name=? COLLATE NOCASE", (name,)).fetchone()
        if dup:
            conn.rollback()
            raise AuthError("昵称已被占用，请换一个。", 409)

        user_id = _new_user_id()
        token = new_token()
        pin = _gen_unique_pin(conn)
        now = _now()
        conn.execute(
            "INSERT INTO users(user_id, name, token_hash, role, disabled,"
            " created_at, last_seen, invite_code, pin_hash) VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, name, hash_token(token), "user", 0, now, None, code, hash_pin(pin)),
        )
        conn.execute("UPDATE invite_codes SET used=used+1 WHERE code=?", (code,))
        conn.commit()
        return {"user_id": user_id, "token": token, "name": name, "pin": pin}
    except AuthError:
        raise
    except sqlite3.IntegrityError:
        conn.rollback()
        raise AuthError("注册失败，请重试。", 500)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- 邀请码 / 管理


def create_invite(max_uses: int = 1, days: int = 7, note: str = "", cfg=None) -> dict:
    try:
        max_uses = int(max_uses)
    except (TypeError, ValueError):
        max_uses = 1
    max_uses = max(1, min(max_uses, 1000))
    if days is None:
        expires_at = None
    else:
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = 7
        days = max(-3650, min(days, 3650))
        expires_at = (
            datetime.now(timezone.utc).replace(microsecond=0)
            + timedelta(days=days)
        ).isoformat()
    note = (note or "").strip()[:200]
    created_at = _now()

    conn = connect(cfg)
    try:
        code = _new_code(conn)
        conn.execute(
            "INSERT INTO invite_codes(code, max_uses, used, expires_at, note, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (code, max_uses, 0, expires_at, note, created_at),
        )
        conn.commit()
        return {
            "code": code,
            "max_uses": max_uses,
            "used": 0,
            "expires_at": expires_at,
            "note": note,
            "created_at": created_at,
        }
    finally:
        conn.close()


def list_invites(cfg=None) -> dict:
    conn = connect(cfg)
    try:
        rows = conn.execute(
            "SELECT code, max_uses, used, expires_at, note, created_at, disabled"
            " FROM invite_codes ORDER BY created_at DESC, code"
        ).fetchall()
        items = [_invite_public(r) for r in rows]
        return {"items": items, "count": len(items)}
    finally:
        conn.close()


def get_invite(code: str, cfg=None):
    conn = connect(cfg)
    try:
        row = _find_invite(conn, code)
        return _invite_public(row) if row else None
    finally:
        conn.close()


def update_invite(code: str, changes: dict, cfg=None) -> dict:
    """修改邀请码：max_uses / expires_at / disabled，只改传入字段。"""
    if not isinstance(changes, dict):
        raise AuthError("请求体必须是 JSON 对象。")
    conn = connect(cfg)
    try:
        row = _find_invite(conn, code)
        if row is None:
            raise AuthError("邀请码不存在。", 404)
        sets, params = [], []
        if "max_uses" in changes:
            try:
                max_uses = int(changes.get("max_uses"))
            except (TypeError, ValueError):
                raise AuthError("max_uses 必须是整数。")
            max_uses = max(0, min(max_uses, 1000000))
            sets.append("max_uses=?")
            params.append(max_uses)
        if "expires_at" in changes:
            sets.append("expires_at=?")
            params.append(_normalize_expires(changes.get("expires_at")))
        if "disabled" in changes:
            value = changes.get("disabled")
            if isinstance(value, str):
                value = value.strip().lower() in ("1", "true", "yes", "on")
            sets.append("disabled=?")
            params.append(1 if value else 0)
        if sets:
            params.append(row["code"])
            conn.execute(
                "UPDATE invite_codes SET %s WHERE code=?" % ", ".join(sets), params
            )
            conn.commit()
        current = _find_invite(conn, row["code"])
        return _invite_public(current)
    finally:
        conn.close()


def list_users(cfg=None) -> list:
    conn = connect(cfg)
    try:
        rows = conn.execute(
            "SELECT user_id, name, role, disabled, created_at, last_seen, invite_code"
            " FROM users ORDER BY created_at, user_id"
        ).fetchall()
        return [
            {
                "user_id": r["user_id"],
                "name": r["name"],
                "role": r["role"],
                "disabled": bool(r["disabled"]),
                "created_at": r["created_at"],
                "last_seen": r["last_seen"],
                "invite_code": r["invite_code"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def stats(cfg=None) -> dict:
    """管理后台概览：用户数 / 今日活跃 / 管理员数。"""
    conn = connect(cfg)
    try:
        users = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        admins = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND disabled=0"
        ).fetchone()["n"]
        active_today = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE disabled=0 AND last_seen IS NOT NULL"
            " AND date(last_seen, 'localtime') = date('now', 'localtime')"
        ).fetchone()["n"]
        return {"users": int(users), "admins": int(admins), "active_today": int(active_today)}
    finally:
        conn.close()


def set_disabled(user_id: str, disabled: bool, cfg=None) -> dict:
    conn = connect(cfg)
    try:
        cur = conn.execute(
            "UPDATE users SET disabled=? WHERE user_id=?", (1 if disabled else 0, user_id)
        )
        conn.commit()
        if cur.rowcount == 0:
            raise AuthError("用户不存在。", 404)
    finally:
        conn.close()
    return get_user(user_id, cfg)


def login(name: str, pin: str, cfg=None) -> dict:
    """昵称 + 4 位口令登录；成功后轮换令牌。"""
    name = " ".join((name or "").split())
    pin = (pin or "").strip()
    if not name or not pin:
        raise AuthError("请输入昵称和口令。", 400)
    conn = connect(cfg)
    try:
        now = time.time()
        fails = conn.execute(
            "SELECT COUNT(*) AS n FROM login_fails WHERE name=? COLLATE NOCASE AND ts>?",
            (name, now - 600),
        ).fetchone()["n"]
        if fails >= 5:
            raise AuthError("尝试次数过多，请 10 分钟后再试。", 429)

        row = conn.execute("SELECT * FROM users WHERE name=? COLLATE NOCASE", (name,)).fetchone()
        ok = bool(row) and bool(row["pin_hash"]) and secrets.compare_digest(
            str(row["pin_hash"]), hash_pin(pin)
        )
        if not ok:
            conn.execute("INSERT INTO login_fails(name, ts) VALUES (?,?)", (name, now))
            conn.commit()
            raise AuthError("昵称或口令不正确。", 401)
        if int(row["disabled"]):
            raise AuthError("账号已被禁用。", 403)

        token = new_token()
        conn.execute(
            "UPDATE users SET token_hash=?, last_seen=? WHERE user_id=?",
            (hash_token(token), _now(), row["user_id"]),
        )
        conn.execute("DELETE FROM login_fails WHERE name=? COLLATE NOCASE", (name,))
        conn.commit()
        return {"user_id": row["user_id"], "name": row["name"], "role": row["role"], "token": token}
    finally:
        conn.close()
