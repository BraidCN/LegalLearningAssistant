"""向量检索：Ollama 嵌入 + float16 .npy 索引（只用标准库）。

设计要点：
- 索引是 numpy 格式的 .npy（float16 向量 / int64 条文 id），但读写完全用
  struct + array 实现，不依赖 numpy；float16 用 struct 的 'e' 格式解码。
- load_index() 懒加载并缓存（按文件 mtime 失效），索引缺失/损坏一律返回 None，
  vector_search() 随之返回 []，检索链路自动退回 FTS。
- 打分用余弦相似度；bge-m3 输出本身是单位向量，索引加载时抽样判断是否需要
  逐行归一化（需要时才算行范数）。

对外接口：
  embed_texts(texts, cfg=None)           -> [[float, ...], ...]
  embed_query(text, cfg=None)            -> [float, ...]（失败返回 []）
  load_index(cfg=None)                   -> dict | None
  vector_search(q, k=None, conn=None, cfg=None) -> [{"article_id", "score", "source"}]
  cosine(a, b)                           -> float
  npy_header(descr, shape)               -> bytes（写 .npy 用）
  read_npy(path)                         -> (descr, shape, payload)
"""
from __future__ import annotations

import ast
import array
import http.client
import json
import math
import os
import struct
import sys
import time
import urllib.error
import urllib.request
from operator import mul
from pathlib import Path

from .config import ROOT, load_config

DEFAULT_EMBEDDING = {
    "enabled": True,
    "provider": "ollama",
    "model": "bge-m3",
    "batch": 32,
    "vectors": "data/db/embeddings.f16.npy",
    "ids": "data/db/ids.npy",
    "top_k": 50,
}

# 单条文本送去嵌入时的上限，防止超长条文撑爆上下文
MAX_TEXT_CHARS = 4000

# 单次请求的总字符预算：Ollama 的 /api/embed 会按「整批文本」占用上下文，
# 一批长条文很容易超 8192 token 而返回 413（input length exceeds the context length），
# 所以既按条数批量，也按字符预算切小批。
MAX_BATCH_CHARS = 6000

# 查询嵌入的超时上限（秒）：检索链路不能因为 Ollama 卡住而长时间阻塞
QUERY_TIMEOUT = 30

# 这些状态码/异常按「可退让」处理：重试 + 折半降批，而不是直接失败
_SOFT_HTTP = {400, 408, 413, 429, 500, 502, 503, 504}
_SOFT_EXC = (urllib.error.URLError, ConnectionError, TimeoutError, OSError, http.client.HTTPException)


class _SoftEmbedError(Exception):
    """可退让的嵌入失败：换个更小的批次/重试大概率能成。"""

_NPY_MAGIC = b"\x93NUMPY"
_INDEX_CACHE: dict = {}


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def embedding_config(cfg: dict | None = None) -> dict:
    """合并默认值与 configs/config.json 的 embedding 段。"""
    base = dict(DEFAULT_EMBEDDING)
    if cfg is None:
        try:
            cfg = load_config()
        except Exception:
            cfg = {}
    section = (cfg or {}).get("embedding") or {}
    if isinstance(section, dict):
        base.update({k: v for k, v in section.items() if v is not None})
    return base


def _abs_path(value) -> Path:
    p = Path(str(value))
    return p if p.is_absolute() else (ROOT / p)


def index_paths(cfg: dict | None = None):
    emb = embedding_config(cfg)
    return _abs_path(emb["vectors"]), _abs_path(emb["ids"])


def _ollama_url(cfg: dict | None) -> str:
    url = ""
    if isinstance(cfg, dict):
        url = ((cfg.get("llm") or {}).get("ollama_url") or "")
    return (url or os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/")


def _timeout(cfg: dict | None) -> float:
    try:
        return float(((cfg or {}).get("llm") or {}).get("timeout") or 120)
    except (TypeError, ValueError):
        return 120.0


# --------------------------------------------------------------------------
# 嵌入
# --------------------------------------------------------------------------
def _post_json(url: str, payload: dict, timeout: float):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _clean(text) -> str:
    t = " ".join(str(text or "").split())
    return t[:MAX_TEXT_CHARS]


def _err_body(exc):
    try:
        return exc.read().decode("utf-8", "replace")[:120]
    except Exception:
        return ""


def _parse_vectors(out, expected):
    vecs = (out or {}).get("embeddings")
    if (isinstance(vecs, list) and len(vecs) == expected
            and all(isinstance(v, list) and v for v in vecs)):
        return vecs
    return None


def _embed_once(texts, model, url, timeout, use_batch):
    """单次请求。返回 (vectors, use_batch)；软失败抛 _SoftEmbedError。"""
    if use_batch:
        try:
            out = _post_json(url + "/api/embed", {"model": model, "input": texts}, timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 405):
                use_batch = False  # 老版本 Ollama 没有批量接口，退回单条
            elif exc.code in _SOFT_HTTP:
                raise _SoftEmbedError("HTTP %d %s" % (exc.code, _err_body(exc)))
            else:
                raise RuntimeError("HTTP %d" % exc.code)
        except _SOFT_EXC as exc:
            raise _SoftEmbedError(repr(exc))
        else:
            vecs = _parse_vectors(out, len(texts))
            if vecs is not None:
                return vecs, True
            use_batch = False  # 返回结构不对，退回单条

    out_vecs = []
    for t in texts:
        try:
            res = _post_json(url + "/api/embeddings", {"model": model, "prompt": t}, timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in _SOFT_HTTP:
                raise _SoftEmbedError("HTTP %d %s" % (exc.code, _err_body(exc)))
            raise RuntimeError("HTTP %d" % exc.code)
        except _SOFT_EXC as exc:
            raise _SoftEmbedError(repr(exc))
        vec = (res or {}).get("embedding")
        if not isinstance(vec, list) or not vec:
            raise RuntimeError("/api/embeddings 返回结构异常")
        out_vecs.append(vec)
    return out_vecs, False


def _embed_shrink(text, model, url, timeout, retries):
    """单条文本仍撞上下文上限时，截短前缀重试（每次减半，最低 200 字）。"""
    keep = len(text)
    last = None
    while keep >= 200:
        try:
            vecs, _ = _embed_once([text[:keep]], model, url, timeout, True)
            if keep < len(text):
                sys.stderr.write("\n[vector] 条文过长（%d 字），已截断到 %d 字嵌入\n"
                                 % (len(text), keep))
            return vecs[0]
        except _SoftEmbedError as exc:
            last = exc
            keep //= 2
    raise RuntimeError("嵌入失败（单条截断到 200 字仍失败）：%s" % last)


def _embed_block(texts, model, url, timeout, use_batch, retries=3):
    """带重试与折半降批的嵌入。

    返回 (vectors, use_batch, split)；split=True 表示这块被拆过，
    调用方应把后续批次的条数降下来，避免反复撞同一个上下文上限。
    """
    last = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            vecs, use_batch = _embed_once(texts, model, url, timeout, use_batch)
            return vecs, use_batch, False
        except _SoftEmbedError as exc:
            last = exc
            if attempt < retries:
                time.sleep(min(2.0, 0.4 * attempt))
    if len(texts) == 1:
        return [_embed_shrink(texts[0], model, url, timeout, retries)], use_batch, False
    mid = len(texts) // 2
    head, use_batch, _ = _embed_block(texts[:mid], model, url, timeout, use_batch, retries)
    tail, use_batch, _ = _embed_block(texts[mid:], model, url, timeout, use_batch, retries)
    return head + tail, use_batch, True


def _chunk_texts(texts, size, max_chars: int = MAX_BATCH_CHARS):
    """按条数与字符预算切批：超长条文自然落到小批甚至单条。"""
    batch, chars = [], 0
    for t in texts:
        if batch and (len(batch) >= size or chars + len(t) > max_chars):
            yield batch
            batch, chars = [], 0
        batch.append(t)
        chars += len(t)
    if batch:
        yield batch


def embed_texts(texts, cfg: dict | None = None, batch: int | None = None,
                retries: int = 3, progress=None, timeout: float | None = None):
    """批量嵌入（自适应降批）。返回与输入等长的向量列表。

    - 按 embedding.batch 与 MAX_BATCH_CHARS 切批（Ollama 按整批占用上下文，
      一批长条文会 413 "input length exceeds the context length"）；
    - 413 / 500 / 连接重置等软错误先重试，仍失败就把批次折半，最小到单条；
    - 某块被拆过后，剩余批次条数减半，避免重复撞上限。
    """
    texts = [_clean(t) for t in (texts or [])]
    if not texts:
        return []
    emb = embedding_config(cfg)
    model = str(emb.get("model") or "bge-m3")
    url = _ollama_url(cfg)
    timeout = float(timeout or _timeout(cfg))
    size = max(1, int(batch or emb.get("batch") or 32))
    total = len(texts)
    vectors: list = []
    use_batch = True
    done = 0
    chunks = list(_chunk_texts(texts, size))
    i = 0
    while i < len(chunks):
        vecs, use_batch, split = _embed_block(chunks[i], model, url, timeout, use_batch, retries)
        if split and size > 1:
            size = max(1, size // 2)
            rest = [t for c in chunks[i + 1:] for t in c]
            chunks = chunks[:i + 1] + list(_chunk_texts(rest, size))
        dim = len(vecs[0])
        for v in vecs:
            if len(v) != dim:
                raise RuntimeError("嵌入维度不一致：%d vs %d" % (dim, len(v)))
        vectors.extend(vecs)
        done += len(vecs)
        if progress is not None:
            progress(done, total)
        i += 1
    return vectors


def embed_query(text, cfg: dict | None = None):
    """单条查询嵌入；任何失败都返回 []（检索降级为 FTS）。

    查询走短超时：检索链路上不能因为 Ollama 卡住把整次检索拖到几分钟。
    """
    q = _clean(text)
    if not q:
        return []
    try:
        out = embed_texts([q], cfg=cfg, retries=1,
                          timeout=min(_timeout(cfg), QUERY_TIMEOUT))
        return out[0] if out else []
    except Exception:
        return []


# --------------------------------------------------------------------------
# .npy 读写（float16 / float32 / int64）
# --------------------------------------------------------------------------
_DTYPE = {
    "f2": ("e", 2),
    "f4": ("f", 4),
    "f8": ("d", 8),
    "i8": ("q", 8),
    "i4": ("i", 4),
}


def npy_header(descr: str, shape) -> bytes:
    """生成 numpy v1.0 头部，长度对齐到 64 字节（numpy 自身也这么做）。"""
    shape_t = tuple(int(x) for x in shape)
    meta = "{'descr': '%s', 'fortran_order': False, 'shape': %s, }" % (descr, str(shape_t))
    prefix = _NPY_MAGIC + bytes((1, 0))          # magic + version
    base = len(prefix) + 2                       # + header 长度字段
    need = base + len(meta) + 1                  # + 结尾换行
    pad = (64 - need % 64) % 64                  # 使总长对齐 64 字节（同 numpy）
    header = meta + " " * pad + "\n"
    return prefix + struct.pack("<H", len(header)) + header.encode("latin1")


def read_npy(path):
    """解析 .npy，返回 (descr, shape, payload bytes)。非 .npy 抛 ValueError。"""
    with open(path, "rb") as fh:
        magic = fh.read(6)
        if magic != _NPY_MAGIC:
            raise ValueError("%s 不是 .npy 文件" % path)
        major = fh.read(2)[0]
        hlen = struct.unpack("<H" if major == 1 else "<I",
                             fh.read(2 if major == 1 else 4))[0]
        meta = ast.literal_eval(fh.read(hlen).decode("latin1"))
        payload = fh.read()
    descr = str(meta.get("descr") or "")
    shape = tuple(int(x) for x in (meta.get("shape") or ()))
    return descr, shape, payload


def _decode_array(descr, shape, payload):
    """解码整数型 .npy（ids 用 int64），返回 array 对象。float16 走 struct。"""
    key = descr.lstrip("<>=|")
    if key not in _DTYPE:
        raise ValueError("不支持的 dtype: %s" % descr)
    code, size = _DTYPE[key]
    if code in ("e", "f", "d"):
        raise ValueError("_decode_array 只用于整数数组: %s" % descr)
    count = 1
    for s in shape:
        count *= s
    if count * size != len(payload):
        raise ValueError("数据长度与 shape 不符")
    arr = array.array(code)
    arr.frombytes(payload)
    if descr[:1] == ">" and size > 1:
        arr.byteswap()
    return arr


# --------------------------------------------------------------------------
# 索引
# --------------------------------------------------------------------------
def _file_sig(paths):
    sig = []
    for p in paths:
        try:
            st = p.stat()
            sig.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            return None
    return tuple(sig)


def load_index(cfg: dict | None = None):
    """懒加载向量索引。

    返回 {"ids": [int], "dim": int, "count": int, "raw": bytes,
          "fmt": "<1024e", "stride": int, "norms": list[float] | None}
    缺失或损坏返回 None。
    """
    vpath, ipath = index_paths(cfg)
    sig = _file_sig((vpath, ipath))
    if sig is None:
        return None
    cached = _INDEX_CACHE.get(sig)
    if cached is not None:
        return cached
    try:
        vdescr, vshape, payload = read_npy(vpath)
        idescr, ishape, ipayload = read_npy(ipath)
        if len(vshape) != 2:
            return None
        count, dim = vshape
        if count <= 0 or dim <= 0:
            return None
        ids = _decode_array(idescr, ishape, ipayload)
        if len(ids) != count:
            return None
        key = vdescr.lstrip("<>=|")
        if key not in ("f2", "f4", "f8"):
            return None
        code, size = _DTYPE[key]
        stride = dim * size
        if len(payload) != count * stride:
            return None
        fmt = "<%d%s" % (dim, code)

        # bge-m3 等模型输出已是单位向量：抽样确认，避免每次查询都多算一遍范数
        norms = None
        sample = range(0, count, max(1, count // 20))
        needs_norm = False
        for i in sample:
            row = struct.unpack_from(fmt, payload, i * stride)
            n = math.sqrt(sum(map(mul, row, row)))
            if abs(n - 1.0) > 1e-3:
                needs_norm = True
                break
        if needs_norm:
            norms = []
            for i in range(count):
                row = struct.unpack_from(fmt, payload, i * stride)
                norms.append(math.sqrt(sum(map(mul, row, row))) or 1.0)

        index = {
            "ids": list(ids),
            "dim": dim,
            "count": count,
            "raw": payload,
            "fmt": fmt,
            "stride": stride,
            "norms": norms,
            "vectors_path": str(vpath),
            "ids_path": str(ipath),
        }
    except Exception:
        return None
    _INDEX_CACHE.clear()
    _INDEX_CACHE[sig] = index
    return index


def index_ready(cfg: dict | None = None) -> bool:
    return load_index(cfg) is not None


def cosine(a, b) -> float:
    """余弦相似度；任一为零向量返回 0.0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(map(mul, a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


def vector_search(q, k: int | None = None, conn=None, cfg: dict | None = None):
    """余弦 top-k，返回 [{"article_id", "score", "source": "vector"}]。

    索引缺失或查询嵌入失败返回 []（调用方退回 FTS）。conn 仅为接口对齐保留。
    """
    text = str(q or "").strip()
    if not text:
        return []
    emb = embedding_config(cfg)
    if not emb.get("enabled", True):
        return []
    index = load_index(cfg)
    if index is None:
        return []
    qv = embed_query(text, cfg)
    if not qv:
        return []
    if len(qv) != index["dim"]:
        return []
    limit = int(k or emb.get("top_k") or 50)
    limit = max(1, min(limit, index["count"]))

    qn = math.sqrt(sum(map(mul, qv, qv))) or 1.0
    fmt, stride, norms = index["fmt"], index["stride"], index["norms"]
    scored = []
    for i in range(index["count"]):
        row = struct.unpack_from(fmt, index["raw"], i * stride)
        denom = qn * (norms[i] if norms else 1.0)
        s = sum(map(mul, qv, row)) / denom if denom else 0.0
        scored.append((s, i))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [
        {"article_id": index["ids"][i], "score": round(s, 6), "source": "vector"}
        for s, i in scored[:limit]
    ]
