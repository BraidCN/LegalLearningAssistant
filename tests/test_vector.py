"""向量检索与 RRF 融合的单测（不联网、不依赖 Ollama）。

跑法：.venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
import sqlite3
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import importlib.util

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lawagent import db as dbmod  # noqa: E402
from lawagent import search as search_mod  # noqa: E402
from lawagent import vector  # noqa: E402
from lawagent.segment import segment  # noqa: E402


def _write_npy(path: Path, descr: str, shape, payload: bytes) -> None:
    path.write_bytes(vector.npy_header(descr, shape) + payload)


def _make_doc(aid, law_name="中华人民共和国测试法", article_no="第一条",
              content="测试条文正文", score=10.0, source="fts"):
    return {
        "article_id": aid,
        "article_no": article_no,
        "chapter": None,
        "section": None,
        "content": content,
        "law_name": law_name,
        "status": "有效",
        "score": score,
        "source": source,
    }


class NpyIOTest(unittest.TestCase):
    def test_header_roundtrip_float16(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "v.npy"
            rows = [[0.5, -0.25, 1.0], [0.1, 0.2, 0.3]]
            flat = [x for r in rows for x in r]
            _write_npy(p, "<f2", (2, 3), struct.pack("<6e", *flat))
            descr, shape, payload = vector.read_npy(p)
            self.assertEqual(descr, "<f2")
            self.assertEqual(shape, (2, 3))
            self.assertEqual(len(payload), 12)
            back = struct.unpack("<6e", payload)
            self.assertAlmostEqual(back[0], 0.5, places=3)
            self.assertAlmostEqual(back[5], 0.3, places=3)

    def test_header_is_64_aligned_like_numpy(self):
        head = vector.npy_header("<f2", (10, 4))
        self.assertTrue(head.startswith(b"\x93NUMPY"))
        self.assertEqual(len(head) % 64, 0)

    def test_rejects_non_npy(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.npy"
            p.write_bytes(b"not an npy at all")
            with self.assertRaises(ValueError):
                vector.read_npy(p)


class CosineTest(unittest.TestCase):
    def test_identical(self):
        self.assertAlmostEqual(vector.cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]), 1.0, places=9)

    def test_orthogonal(self):
        self.assertAlmostEqual(vector.cosine([1.0, 0.0], [0.0, 2.0]), 0.0, places=9)

    def test_scale_invariant(self):
        self.assertAlmostEqual(vector.cosine([1.0, 1.0, 0.0], [5.0, 5.0, 0.0]), 1.0, places=9)

    def test_opposite(self):
        self.assertAlmostEqual(vector.cosine([1.0, 0.0], [-1.0, 0.0]), -1.0, places=9)

    def test_degenerate(self):
        self.assertEqual(vector.cosine([], [1.0]), 0.0)
        self.assertEqual(vector.cosine([0.0, 0.0], [1.0, 1.0]), 0.0)
        self.assertEqual(vector.cosine([1.0], [1.0, 2.0]), 0.0)


class RrfTest(unittest.TestCase):
    def test_formula(self):
        fts = [_make_doc(1), _make_doc(2)]
        vec = [_make_doc(2, source="vector"), _make_doc(3, source="vector")]
        fused = search_mod.rrf_fuse([fts, vec], rrf_k=60)
        by_id = {f["article_id"]: f for f in fused}
        self.assertAlmostEqual(by_id[1]["rrf"], round(1 / 61, 8), places=8)
        self.assertAlmostEqual(by_id[3]["rrf"], round(1 / 62, 8), places=8)
        # 两路都命中的排最前：1/61 + 1/62
        self.assertAlmostEqual(by_id[2]["rrf"], round(1 / 62 + 1 / 61, 8), places=8)
        self.assertEqual(fused[0]["article_id"], 2)
        self.assertEqual(by_id[2]["sources"], ["fts", "vector"])

    def test_dedupe_and_order(self):
        fts = [_make_doc(5), _make_doc(6), _make_doc(7)]
        vec = [_make_doc(5, source="vector")]
        fused = search_mod.rrf_fuse([fts, vec])
        self.assertEqual(len(fused), 3)
        self.assertEqual([f["article_id"] for f in fused][0], 5)
        self.assertEqual(fused[0]["sources"], ["fts", "vector"])
        self.assertEqual(fused[1]["sources"], ["fts"])

    def test_rrf_k_changes_scores(self):
        a = search_mod.rrf_fuse([[_make_doc(1)]], rrf_k=1)[0]["rrf"]
        b = search_mod.rrf_fuse([[_make_doc(1)]], rrf_k=60)[0]["rrf"]
        self.assertAlmostEqual(a, 0.5, places=6)
        self.assertAlmostEqual(b, round(1 / 61, 8), places=8)

    def test_empty_and_junk(self):
        self.assertEqual(search_mod.rrf_fuse([]), [])
        self.assertEqual(search_mod.rrf_fuse([[], []]), [])
        self.assertEqual(search_mod.rrf_fuse([[{"no_id": 1}], [None]]), [])

    def test_tie_is_deterministic(self):
        one = search_mod.rrf_fuse([[_make_doc(11)], [_make_doc(12)]])
        two = search_mod.rrf_fuse([[_make_doc(12)], [_make_doc(11)]])
        self.assertEqual([f["article_id"] for f in one], [11, 12])
        self.assertEqual([f["article_id"] for f in one], [f["article_id"] for f in two])


class IndexMissingTest(unittest.TestCase):
    def test_load_index_missing(self):
        cfg = {"embedding": {"vectors": "does/not/exist.npy", "ids": "does/not/exist2.npy"}}
        self.assertIsNone(vector.load_index(cfg))
        self.assertFalse(vector.index_ready(cfg))

    def test_vector_search_degrades_without_index(self):
        # 索引缺失时必须直接返回 []（不得发起网络请求）
        cfg = {"embedding": {"vectors": "nope.npy", "ids": "nope2.npy"}}
        with mock.patch.object(vector, "embed_query") as embed:
            self.assertEqual(vector.vector_search("拖欠工资", k=5, cfg=cfg), [])
            embed.assert_not_called()

    def test_vector_search_with_real_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            vp, ip = Path(tmp) / "v.npy", Path(tmp) / "ids.npy"
            vecs = [[1.0, 0.0], [0.0, 1.0], [0.7071, 0.7071]]
            _write_npy(vp, "<f2", (3, 2), struct.pack("<6e", *[x for v in vecs for x in v]))
            _write_npy(ip, "<i8", (3,), struct.pack("<3q", 101, 102, 103))
            cfg = {"embedding": {"vectors": str(vp), "ids": str(ip), "top_k": 2}}
            with mock.patch.object(vector, "embed_query", return_value=[1.0, 0.05]):
                out = vector.vector_search("查询", k=2, cfg=cfg)
            self.assertEqual([r["article_id"] for r in out], [101, 103])
            self.assertTrue(all(r["source"] == "vector" for r in out))
            self.assertGreater(out[0]["score"], out[1]["score"])

    def test_ids_rows_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            vp, ip = Path(tmp) / "v.npy", Path(tmp) / "ids.npy"
            _write_npy(vp, "<f2", (2, 2), struct.pack("<4e", 1, 0, 0, 1))
            _write_npy(ip, "<i8", (3,), struct.pack("<3q", 1, 2, 3))
            cfg = {"embedding": {"vectors": str(vp), "ids": str(ip)}}
            self.assertIsNone(vector.load_index(cfg))


class _TempDb:
    """建一个最小 articles/laws/articles_fts 库，供 search() 集成测试。"""

    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        dbmod.init(conn)
        conn.execute(
            "INSERT INTO laws(law_id, law_name, status) VALUES (1, ?, '有效')",
            ("中华人民共和国劳动合同法",),
        )
        for aid, no, content in self.rows:
            conn.execute(
                "INSERT INTO articles(article_id, law_id, article_no, content) "
                "VALUES (?, 1, ?, ?)",
                (aid, no, content),
            )
            text = segment("中华人民共和国劳动合同法 %s %s" % (no, content))
            conn.execute(
                "INSERT INTO articles_fts(rowid, search_text) VALUES (?, ?)", (aid, text)
            )
        conn.commit()
        self.conn = conn
        return self

    def cfg(self, **embedding):
        """临时库对应的配置（paths.db 指到临时库，向量路才会启用）。"""
        return {"paths": {"db": str(self.path)}, "embedding": embedding}

    def __exit__(self, *exc):
        self.conn.close()
        self.tmp.cleanup()
        return False


ROWS = [
    (1, "第一条", "为了明确劳动合同双方的权利和义务，保护劳动者的合法权益，制定本法。"),
    (2, "第三十条", "用人单位应当按照劳动合同约定和国家规定，向劳动者及时足额支付劳动报酬。"),
    (3, "第八十五条", "用人单位拖欠劳动报酬的，由劳动行政部门责令限期支付；逾期不支付的加付赔偿金。"),
    (4, "第九十条", "劳动者违反本法规定解除劳动合同，给用人单位造成损失的，应当承担赔偿责任。"),
]


class SearchDegradeTest(unittest.TestCase):
    def test_without_index_same_as_fts(self):
        with _TempDb(ROWS) as db:
            cfg = db.cfg(enabled=True, vectors="nope/v.npy", ids="nope/i.npy")
            rows = search_mod.search("拖欠劳动报酬", k=5, conn=db.conn, cfg=cfg)
            fts_rows = search_mod._fts_pipeline(db.conn, "拖欠劳动报酬", 5)
            self.assertTrue(rows)
            self.assertEqual([r["article_id"] for r in rows],
                             [r["article_id"] for r in fts_rows][:5])
            self.assertTrue(all(r["source"] == "fts" for r in rows))

    def test_disabled_embedding_skips_vector(self):
        with _TempDb(ROWS) as db:
            cfg = db.cfg(enabled=False)
            with mock.patch.object(search_mod.vector, "vector_search") as vs:
                rows = search_mod.search("拖欠劳动报酬", k=3, conn=db.conn, cfg=cfg)
            vs.assert_not_called()
            self.assertTrue(rows)

    def test_foreign_db_skips_vector(self):
        """连的不是索引对应的库时不能融合（否则会掺进别的库的条文）。"""
        with _TempDb(ROWS) as db:
            cfg = {"paths": {"db": "data/db/articles.db"},
                   "embedding": {"enabled": True, "vectors": "nope.npy", "ids": "nope.npy"}}
            with mock.patch.object(search_mod.vector, "vector_search") as vs:
                rows = search_mod.search("拖欠劳动报酬", k=3, conn=db.conn, cfg=cfg)
            vs.assert_not_called()
            self.assertTrue(all(r["source"] == "fts" for r in rows))

    def test_hybrid_fusion_when_vector_available(self):
        with _TempDb(ROWS) as db:
            cfg = db.cfg(enabled=True, top_k=50)
            fake = [
                {"article_id": 4, "score": 0.91, "source": "vector"},
                {"article_id": 3, "score": 0.88, "source": "vector"},
            ]
            with mock.patch.object(search_mod.vector, "vector_search", return_value=fake):
                rows = search_mod.search("拖欠劳动报酬", k=5, conn=db.conn, cfg=cfg)
            ids = [r["article_id"] for r in rows]
            self.assertIn(4, ids)          # 只有向量命中的也必须出现在结果里
            self.assertEqual(len(ids), len(set(ids)))  # article_id 去重
            self.assertEqual(ids[0], 3)    # FTS 与向量都命中的排最前
            top = rows[0]
            self.assertEqual(top["source"], "hybrid")
            self.assertIn("rrf", top)
            # 字段从库里补全
            self.assertEqual(top["law_name"], "中华人民共和国劳动合同法")
            self.assertTrue(top["content"])
            # 纯向量命中的 score 仍是 FTS 量纲（router 依赖）
            vec_only = [r for r in rows if r["article_id"] == 4][0]
            self.assertAlmostEqual(vec_only["score"], round(0.91 * search_mod.VEC_SCORE_SCALE, 4))

    def test_vector_exception_falls_back(self):
        with _TempDb(ROWS) as db:
            cfg = db.cfg(enabled=True)
            with mock.patch.object(search_mod.vector, "vector_search",
                                   side_effect=RuntimeError("ollama down")):
                rows = search_mod.search("拖欠劳动报酬", k=3, conn=db.conn, cfg=cfg)
            self.assertTrue(rows)
            self.assertTrue(all(r["source"] == "fts" for r in rows))


class AdaptiveBatchTest(unittest.TestCase):
    """413（上下文超限）/ 500 / 连接重置时的降批与截断逻辑，全部离线。"""

    def test_chunk_by_char_budget(self):
        texts = ["x" * 4000, "y" * 4000, "z" * 100]
        # 两条 4000 字不能同批（8000 > 6000），但 4000 + 100 可以
        self.assertEqual([len(c) for c in vector._chunk_texts(texts, 8)], [1, 2])
        small = ["a" * 10] * 5
        self.assertEqual([len(c) for c in vector._chunk_texts(small, 3)], [3, 2])

    def test_block_halves_on_413(self):
        def fake(texts, model, url, timeout, use_batch):
            if len(texts) > 2:
                raise vector._SoftEmbedError("HTTP 413 too long")
            return [[float(len(t))] for t in texts], use_batch

        with mock.patch.object(vector, "_embed_once", side_effect=fake):
            with mock.patch.object(vector.time, "sleep"):
                vecs, _, split = vector._embed_block(["a", "bb", "ccc", "dddd"],
                                                     "m", "u", 1, True)
        self.assertTrue(split)
        self.assertEqual(len(vecs), 4)
        self.assertEqual([v[0] for v in vecs], [1.0, 2.0, 3.0, 4.0])

    def test_single_text_shrinks_then_succeeds(self):
        seen = []

        def fake(texts, model, url, timeout, use_batch):
            seen.append(len(texts[0]))
            if len(texts[0]) > 300:
                raise vector._SoftEmbedError("HTTP 413")
            return [[1.0, 0.0]], use_batch

        with mock.patch.object(vector, "_embed_once", side_effect=fake):
            vecs, _, _ = vector._embed_block(["字" * 1200], "m", "u", 1, True)
        self.assertEqual(len(vecs), 1)
        self.assertLessEqual(seen[-1], 300)   # 截短后才成功
        self.assertGreater(len(seen), 1)

    def test_500_recovers_after_retry(self):
        calls = {"n": 0}

        def flaky(texts, model, url, timeout, use_batch):
            calls["n"] += 1
            if calls["n"] == 1:
                raise vector._SoftEmbedError("HTTP 500")
            return [[1.0] for _ in texts], use_batch

        with mock.patch.object(vector, "_embed_once", side_effect=flaky):
            with mock.patch.object(vector.time, "sleep"):
                vecs, _, split = vector._embed_block(["a", "b"], "m", "u", 1, True)
        self.assertFalse(split)
        self.assertEqual(len(vecs), 2)

    def test_embed_texts_keeps_order_after_split(self):
        def fake(texts, model, url, timeout, use_batch):
            if len(texts) > 3:
                raise vector._SoftEmbedError("HTTP 413")
            return [[float(len(t))] for t in texts], use_batch

        texts = ["a" * (i + 1) for i in range(10)]
        with mock.patch.object(vector, "_embed_once", side_effect=fake):
            with mock.patch.object(vector.time, "sleep"):
                vecs = vector.embed_texts(texts, cfg={"embedding": {"batch": 8}})
        self.assertEqual([v[0] for v in vecs], [float(i + 1) for i in range(10)])


class BuildResumeTest(unittest.TestCase):
    """build/embed.py 的增量落盘与断点续跑。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.vp, self.ip = d / "v.npy", d / "ids.npy"
        self.texts = ["条文%d" % i for i in range(10)]
        self.ids = list(range(101, 111))
        spec = importlib.util.spec_from_file_location("embed_build", ROOT / "build" / "embed.py")
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.cfg = {"embedding": {"vectors": str(self.vp), "ids": str(self.ip)}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_partial_index_rejected_then_resumed(self):
        calls = {"n": 0}

        def fake(part, cfg=None, batch=None, retries=3, progress=None):
            calls["n"] += 1
            if calls["n"] == 2:          # 第二批复现 413 场景直接炸
                raise RuntimeError("boom: HTTP 413")
            return [[1.0, float(len(t))] for t in part]

        with mock.patch.object(self.mod.vector, "embed_texts", side_effect=fake):
            with self.assertRaises(RuntimeError):
                self.mod.build_index(self.texts, self.ids, self.vp, self.ip, {}, chunk=4)
        # 半成品：header 声明 10 行、实际 4 行 -> 不能用于检索
        self.assertIsNone(vector.load_index(self.cfg))
        self.assertEqual(self.mod._index_state(self.vp, self.ip, self.ids)[0], 4)

        with mock.patch.object(self.mod.vector, "embed_texts",
                               side_effect=lambda part, **kw: [[2.0, 2.0] for _ in part]) as m:
            info = self.mod.build_index(self.texts, self.ids, self.vp, self.ip, {}, chunk=4)
        self.assertEqual(info["resumed"], 4)
        self.assertEqual(info["count"], 10)
        self.assertEqual(m.call_count, 2)          # 只补剩下 6 条（4 + 2）
        idx = vector.load_index(self.cfg)
        self.assertEqual(idx["count"], 10)
        self.assertEqual(idx["ids"], self.ids)
        self.assertEqual(idx["dim"], 2)
        # 断点记录只保留已完成部分，完成后整份对齐
        _, ishape, _ = vector.read_npy(self.ip)
        self.assertEqual(ishape, (10,))


if __name__ == "__main__":
    unittest.main()
