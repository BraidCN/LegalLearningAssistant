"""建立向量索引：条文全文 -> Ollama 嵌入 -> float16 .npy + ids.npy。

用法：
  python build/embed.py                    # 全量（24k 条，几分钟）
  python build/embed.py --limit 500        # 抽样验证，写到 *.sample.*.npy
  python build/embed.py --batch 16 --db data/db/articles.db

嵌入文本格式：``《法名》第X条｜正文``（法名+条号帮助消歧，正文被截断到 4000 字）。
向量按 ``article_id`` 升序落盘，ids.npy 与行一一对齐。

加 --limit N 时默认写到 ``embeddings.f16.sample.npy`` / ``ids.sample.npy``，
避免半成品索引被检索链误用；位置可用 --out / --out-ids 覆盖。
"""
from __future__ import annotations

import argparse
import os
import random
import sqlite3
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lawagent import vector  # noqa: E402
from lawagent.config import load_config  # noqa: E402

SQL = (
    "SELECT a.article_id, a.article_no, a.content, l.law_name "
    "FROM articles a JOIN laws l ON l.law_id = a.law_id "
    "ORDER BY a.article_id"
)


def article_text(law_name: str, article_no: str, content: str) -> str:
    """拼嵌入文本：《法名》第X条｜正文。"""
    body = " ".join((content or "").split())
    return "《%s》%s｜%s" % (law_name or "", article_no or "", body)


def load_rows(db_path: Path, limit: int = 0):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        sql = SQL + (" LIMIT ?" if limit else "")
        rows = conn.execute(sql, (limit,) if limit else ()).fetchall()
        return [(int(r["article_id"]),
                 article_text(r["law_name"], r["article_no"], r["content"]))
                for r in rows]
    finally:
        conn.close()


def _sample_paths(cfg: dict, limit: int):
    vpath, ipath = vector.index_paths(cfg)
    if not limit:
        return vpath, ipath
    return (vpath.with_name(vpath.stem + ".sample.npy"),
            ipath.with_name(ipath.stem + ".sample.npy"))


def _progress_printer(total: int):
    t0 = time.time()

    def report(done, tot):
        elapsed = max(1e-6, time.time() - t0)
        rate = done / elapsed
        left = (tot - done) / rate if rate else 0
        sys.stderr.write("\r嵌入 %d/%d（%.1f%%）%.1f 条/秒 剩余约 %s" % (
            done, tot, 100.0 * done / max(1, tot), rate, _fmt_eta(left)))
        sys.stderr.flush()
        if done >= tot:
            sys.stderr.write("\n")

    return report


def _fmt_eta(seconds: float) -> str:
    s = int(max(0, seconds))
    if s >= 3600:
        return "%dh%02dm" % (s // 3600, s % 3600 // 60)
    if s >= 60:
        return "%dm%02ds" % (s // 60, s % 60)
    return "%ds" % s


def _write_ids(ipath: Path, ids) -> None:
    """写 ids.npy；每完成一批就重写一次，兼作断点记录。"""
    tmp = ipath.with_suffix(ipath.suffix + ".part")
    with open(tmp, "wb") as fh:
        fh.write(vector.npy_header("<i8", (len(ids),)))
        fh.write(struct.pack("<%dq" % len(ids), *[int(i) for i in ids]))
    os.replace(tmp, ipath)


def _index_state(vpath: Path, ipath: Path, ids):
    """检查已有索引：返回 (已完成行数, 维度)；不可续跑返回 (0, 0)。

    续跑条件：向量文件 dtype/shape 与本次一致、ids 前缀与本次一致、
    已完成行数 = min(向量行数, ids 条数)。
    """
    if not (vpath.exists() and ipath.exists()):
        return 0, 0
    try:
        vdescr, vshape, vpayload = vector.read_npy(vpath)
        idescr, ishape, ipayload = vector.read_npy(ipath)
        if vdescr != "<f2" or len(vshape) != 2 or vshape[0] != len(ids):
            return 0, 0
        dim = vshape[1]
        rows = len(vpayload) // (dim * 2)
        id_arr = vector._decode_array(idescr, ishape, ipayload)
    except Exception:
        return 0, 0
    done = min(rows, len(id_arr), len(ids))
    if done <= 0:
        return 0, 0
    if list(id_arr)[:done] != [int(i) for i in ids[:done]]:
        return 0, 0
    return done, dim


def build_index(texts, ids, vpath: Path, ipath: Path, cfg, batch=None,
                chunk: int = 512, progress=None, resume: bool = True) -> dict:
    """边嵌入边落盘（float16 .npy + int64 ids.npy），支持断点续跑。

    每完成一个 chunk 就写盘并重写 ids.npy，所以中断后用同样的参数重跑即可
    从已完成的行继续。未写完的索引在 load_index() 里会因为行数对不上被判定
    为不可用，检索链自动退回 FTS，不会读到半成品。
    """
    if not texts or len(texts) != len(ids):
        raise ValueError("texts 与 ids 数量不一致：%d vs %d" % (len(texts), len(ids)))
    vpath.parent.mkdir(parents=True, exist_ok=True)
    total = len(texts)
    done, dim = _index_state(vpath, ipath, ids) if resume else (0, 0)
    if resume and done:
        print("检测到已完成 %d/%d 行，从断点续跑（dim=%d）" % (done, total, dim))
    if not done:
        dim = 0
        for p in (vpath, ipath):  # 不能续跑就从头来
            try:
                p.unlink()
            except OSError:
                pass

    mode = "r+b" if (done and vpath.exists()) else "wb"
    written = done
    with open(vpath, mode) as fh:
        if done:
            header_len = len(vector.npy_header("<f2", (total, dim)))
            fh.seek(header_len + done * dim * 2)
            fh.truncate()
        while written < total:
            part = texts[written:written + chunk]
            vecs = vector.embed_texts(part, cfg=cfg, batch=batch)
            if not vecs:
                raise RuntimeError("第 %d 条起返回空向量" % (written + 1))
            if not dim:
                dim = len(vecs[0])
                fh.seek(0)
                fh.truncate()
                fh.write(vector.npy_header("<f2", (total, dim)))
            for v in vecs:
                if len(v) != dim:
                    raise RuntimeError("向量维度不一致：%d vs %d" % (len(v), dim))
            flat = [x for v in vecs for x in v]
            fh.write(struct.pack("<%de" % len(flat), *flat))
            fh.flush()
            os.fsync(fh.fileno())
            written += len(vecs)
            _write_ids(ipath, ids[:written])  # 断点记录
            if progress is not None:
                progress(written, total)

    _write_ids(ipath, ids)
    return {"count": written, "dim": dim, "resumed": done}


def verify(vpath: Path, ipath: Path, texts, ids, cfg: dict, probes: int = 3) -> dict:
    """回读索引：行数、ids 对齐，并随机抽 3 条做「自相似 top1 命中」检查。"""
    cfg2 = dict(cfg)
    cfg2["embedding"] = dict(vector.embedding_config(cfg))
    cfg2["embedding"]["vectors"] = str(vpath)
    cfg2["embedding"]["ids"] = str(ipath)
    index = vector.load_index(cfg2)
    info = {
        "count": 0, "dim": 0, "ids_len": 0, "ids0": None,
        "checks": [], "self_cos": None,
    }
    try:
        idescr, ishape, ipayload = vector.read_npy(ipath)
        info["ids_len"] = list(ishape)[0] if ishape else 0
    except Exception:
        pass
    if not index:
        return info
    info["count"] = index["count"]
    info["dim"] = index["dim"]
    info["ids0"] = index["ids"][0]
    picks = random.sample(range(len(texts)), min(probes, len(texts)))
    for i in picks:
        qv = vector.embed_query(texts[i], cfg2)
        if not qv:
            info["checks"].append({"row": i, "expect": ids[i], "top1": None, "self_cos": None})
            continue
        row = struct.unpack_from(index["fmt"], index["raw"], i * index["stride"])
        cos = round(vector.cosine(qv, list(row)), 4)
        hits = vector.vector_search(texts[i], k=1, cfg=cfg2)
        info["checks"].append({
            "row": i,
            "expect": ids[i],
            "top1": hits[0]["article_id"] if hits else None,
            "self_cos": cos,
        })
    if info["checks"]:
        info["self_cos"] = info["checks"][0]["self_cos"]
    return info


def main(argv=None):
    ap = argparse.ArgumentParser(description="建立条文向量索引（Ollama bge-m3）")
    ap.add_argument("--limit", type=int, default=0, help="只嵌入前 N 条（抽样验证）")
    ap.add_argument("--db", default="", help="sqlite 路径，默认取 config paths.db")
    ap.add_argument("--batch", type=int, default=0, help="覆盖 embedding.batch")
    ap.add_argument("--chunk", type=int, default=512, help="每批落盘条数（内存上限）")
    ap.add_argument("--model", default="", help="覆盖 embedding.model")
    ap.add_argument("--out", default="", help="覆盖向量 .npy 输出路径")
    ap.add_argument("--out-ids", default="", help="覆盖 ids .npy 输出路径")
    ap.add_argument("--no-resume", action="store_true", help="忽略已有文件，从头嵌入")
    args = ap.parse_args(argv)

    cfg = load_config()
    emb = vector.embedding_config(cfg)
    if args.model:
        cfg.setdefault("embedding", {})["model"] = args.model
        emb["model"] = args.model
    db_path = Path(args.db) if args.db else (ROOT / str((cfg.get("paths") or {}).get("db") or "data/db/articles.db"))
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    vpath, ipath = _sample_paths(cfg, args.limit)
    if args.out:
        vpath = Path(args.out)
    if args.out_ids:
        ipath = Path(args.out_ids)

    print("数据库：%s" % db_path)
    print("模型：%s（%s） 批次：%d" % (emb["model"], vector._ollama_url(cfg),
                                    args.batch or emb.get("batch") or 32))
    rows = load_rows(db_path, args.limit)
    if not rows:
        print("没有读到条文，退出。", file=sys.stderr)
        return 1
    ids = [r[0] for r in rows]
    texts = [r[1] for r in rows]
    print("待嵌入：%d 条；示例：%s" % (len(texts), texts[0][:60]))

    t0 = time.time()
    try:
        info_build = build_index(
            texts, ids, vpath, ipath, cfg,
            batch=args.batch or None,
            chunk=args.chunk,
            progress=_progress_printer(len(texts)),
            resume=not args.no_resume,
        )
    except Exception as exc:
        print("\n嵌入失败：%s" % exc, file=sys.stderr)
        print("已完成部分已落盘，修好环境后重跑同一命令会自动续跑。", file=sys.stderr)
        return 2
    elapsed = time.time() - t0
    print("嵌入完成：%d 条 × %d 维，耗时 %.1fs（%.1f 条/秒），本次新增 %d 条" % (
        info_build["count"], info_build["dim"], elapsed,
        (info_build["count"] - info_build["resumed"]) / max(1e-6, elapsed),
        info_build["count"] - info_build["resumed"]))

    print("已写入：\n  %s（%.1f MB）\n  %s" % (
        vpath, vpath.stat().st_size / 1e6, ipath))

    # 校验：ids 长度、向量行数、随机 3 条自相似 top1
    info = verify(vpath, ipath, texts, ids, cfg)
    print("校验：向量 %d 行、ids %d 条、%d 维；首条 self_cos=%s" % (
        info["count"], info["ids_len"], info["dim"], info["self_cos"]))
    for c in info["checks"]:
        print("  抽检 row=%d expect=%s top1=%s self_cos=%s %s" % (
            c["row"], c["expect"], c["top1"], c["self_cos"],
            "OK" if c["top1"] == c["expect"] else "MISS"))
    ok = (info["count"] == len(texts)
          and info["ids_len"] == len(texts)
          and all(c["top1"] == c["expect"] for c in info["checks"]))
    if not ok:
        print("！校验未通过（行数或抽检不一致）", file=sys.stderr)
        return 3
    print("校验通过：%d 行、%d 维，抽检 %d 条 top1 全部命中。" % (
        info["count"], info["dim"], len(info["checks"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
