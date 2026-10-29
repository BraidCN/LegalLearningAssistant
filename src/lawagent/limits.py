"""联网版并发控制：一个生成位 + 有限队列 + 每用户限流。

规则（尽量简单）：
- 同时只跑 1 个生成请求；
- 最多 2 个排队，第 4 个人直接收到 429；
- 排队超过 120 秒仍未轮到 → 429；
- 排队超过 25 秒 → 该请求改用云端模型（本地继续服务排在前面的）；
- 每人每分钟 5 次、每小时 20 次。

云端只在“本地模型报错/超时”时由 pipeline 自己回退，不做排队换云。
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager

MAX_CONCURRENT = 1
MAX_QUEUE = int(os.environ.get("LAW_MAX_QUEUE", "2"))
ACQUIRE_TIMEOUT = float(os.environ.get("LAW_QUEUE_TIMEOUT", "120"))
SLOW_AFTER = float(os.environ.get("LAW_SLOW_AFTER", "25"))  # 排队超过这个秒数就改用云端
RATE_PER_MIN = int(os.environ.get("LAW_RATE_MIN", "5"))
RATE_PER_HOUR = int(os.environ.get("LAW_RATE_HOUR", "20"))
# 匿名访客（未登录）按 IP 计：宽松一些，校园网 NAT 后面可能多人共用一个出口
ANON_PER_MIN = int(os.environ.get("LAW_ANON_RATE_MIN", "8"))
ANON_PER_HOUR = int(os.environ.get("LAW_ANON_RATE_HOUR", "120"))

_sem = threading.Semaphore(MAX_CONCURRENT)
_lock = threading.Lock()
_waiting = {"n": 0}
_runs = {"n": 0}
_rate: dict[str, deque] = defaultdict(deque)


class Busy(Exception):
    """并发或排队已满。"""


def stats() -> dict:
    with _lock:
        return {"running": _runs["n"], "waiting": _waiting["n"],
                "max_concurrent": MAX_CONCURRENT, "max_queue": MAX_QUEUE}


def rate_check(uid: str, anon: bool = False) -> tuple[bool, int]:
    now = time.time()
    dq = _rate[uid]
    while dq and now - dq[0] > 3600:
        dq.popleft()
    per_min = ANON_PER_MIN if anon else RATE_PER_MIN
    per_hour = ANON_PER_HOUR if anon else RATE_PER_HOUR
    recent = [t for t in dq if now - t < 60]
    if len(recent) >= per_min:
        return False, max(5, 60 - int(now - recent[0]))
    if len(dq) >= per_hour:
        return False, max(5, 3600 - int(now - dq[0]))
    dq.append(now)
    return True, 0


@contextmanager
def slot():
    with _lock:
        if _waiting["n"] >= MAX_QUEUE:
            raise Busy()
        _waiting["n"] += 1
    t0 = time.time()
    ok = _sem.acquire(timeout=ACQUIRE_TIMEOUT)
    with _lock:
        _waiting["n"] -= 1
        if ok:
            _runs["n"] += 1
    if not ok:
        raise Busy()
    try:
        yield time.time() - t0
    finally:
        with _lock:
            _runs["n"] -= 1
        _sem.release()
