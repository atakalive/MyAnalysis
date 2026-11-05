"""llm_bridge/chat_index.py — チャットの埋め込み索引（Issue #125）。

埋め込みは FakeEmbedder（本文の sha256 から決定的なベクトル）。索引の置き場は conftest が tmp に
向けている。データセットは config.register_dataset で登録する（update が reload_datasets を呼ぶので、
config.DATASETS に直接足した分は消える）。
"""
from __future__ import annotations

import hashlib
import io
import itertools
import json
import os
import shutil
import types

import numpy as np
import pytest

import config
from common import embed_model, filelock
from common import paths as cpaths
from llm_backend.base import Message
from llm_bridge import chat_index as ci
from llm_bridge import chat_store
from llm_bridge import paths as lb_paths


class FakeEmbedder:
    def __init__(self, dims=8, model_id="fake-8", recipe=None):
        self.dims, self.model_id, self.recipe = dims, model_id, recipe
        self.calls: list[str] = []
        self.queries: list[str] = []
        self.query_vec = None
        self.after_batch = None       # 4 件ごとのバッチの後に呼ぶ（時計を進める・cancel を立てる）
        self.closed = False

    def vec(self, text: str) -> np.ndarray:
        h = hashlib.sha256(text.encode("utf-8")).digest()
        v = np.frombuffer(h, dtype=np.uint8)[: self.dims].astype(np.float32) - 127.5
        return v / np.linalg.norm(v)

    def embed_documents(self, texts, *, progress=None, cancel=None):
        out = np.zeros((len(texts), self.dims), dtype=np.float32)
        for start in range(0, len(texts), 4):
            if cancel is not None and cancel():
                raise ci.Cancelled()
            for i in range(start, min(start + 4, len(texts))):
                out[i] = self.vec(texts[i])
                self.calls.append(texts[i])
            if self.after_batch is not None:
                self.after_batch()
            if progress is not None:
                progress(min(start + 4, len(texts)), len(texts))
        return out

    def embed_query(self, text):
        self.queries.append(text)
        return self.query_vec if self.query_vec is not None else self.vec(text)

    def close(self):
        self.closed = True


# ---------------------------------------------------------------- 道具立て


def _register(tmp_path, name: str):
    d = tmp_path / "data" / name
    d.mkdir(parents=True, exist_ok=True)
    config.register_dataset(name, str(d))
    config.reload_datasets()
    return d


def _chat(ds_dir, msgs, *, sid=None, title="t", archived=False, kind="chat", updated=None,
          work="_work"):
    s = chat_store.new_session("mock", "system prompt", title=title, kind=kind)
    if sid is not None:
        s.id = sid
    for role, content in msgs:
        s.messages.append(Message(role=role, content=content))
    s.archived = archived
    if updated is not None:
        s.updated = updated
    chat_store.write_session_file(ds_dir / work, s)
    return s


def _cs(ds_dir, work="_work"):
    return ds_dir / work / "chat_sessions"


def _bump(path, ns=2_000_000_000):
    """stat を明示的にずらす（Windows の時刻は約 15.6 ms 刻み）。"""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + ns))


def _shard(name, slug="fake-8"):
    return lb_paths.chat_index_dir() / slug / ci.dskey(name)


def _manifest(name, slug="fake-8"):
    return json.loads((_shard(name, slug) / "manifest.json").read_text(encoding="utf-8"))


@pytest.fixture
def emb():
    return FakeEmbedder()


@pytest.fixture
def idx(emb):
    return ci.ChatIndex(emb)


SID_A = "aaaaaaaa" + "0" * 24
SID_B = "bbbbbbbb" + "0" * 24
SID_C = "cccccccc" + "0" * 24


# ---------------------------------------------------------------- 設定・準備状態


def test_load_prefs_defaults_and_bad_values():
    p = ci.load_prefs()
    assert p == ci.RecallPrefs(False, embed_model.DEFAULT_MODEL, True)
    lb_paths.update_ui_pref("chat_recall_enabled", "yes")
    lb_paths.update_ui_pref("chat_recall_model", "no-such")
    lb_paths.update_ui_pref("chat_recall_in_ai_search", 0)
    assert ci.load_prefs() == ci.RecallPrefs(False, embed_model.DEFAULT_MODEL, True)
    lb_paths.update_ui_pref("chat_recall_enabled", True)
    lb_paths.update_ui_pref("chat_recall_model", "multilingual-e5-small")
    lb_paths.update_ui_pref("chat_recall_in_ai_search", False)
    assert ci.load_prefs() == ci.RecallPrefs(True, "multilingual-e5-small", False)


@pytest.mark.parametrize("enabled,deps,state,ready", [
    (False, [], "installed", False),
    (True, ["onnxruntime"], "installed", False),
    (True, [], "missing", False),
    (True, [], "installed", True),
])
def test_is_ready(monkeypatch, enabled, deps, state, ready):
    lb_paths.update_ui_pref("chat_recall_enabled", enabled)
    model = embed_model.CATALOG[embed_model.DEFAULT_MODEL]
    monkeypatch.setattr(embed_model, "readiness",
                        lambda mid: embed_model.Readiness(list(deps), state, model))
    assert ci.is_ready() is ready


def test_make_embedder_cache_and_release(monkeypatch):
    with pytest.raises(ci.NotReady):
        ci.make_embedder()
    made = []

    class Fake:
        def __init__(self, model, root):
            self.model_id = model.id
            self.root = root
            self.closed = False
            made.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(embed_model, "OnnxEmbedder", Fake)
    lb_paths.update_ui_pref("chat_recall_enabled", True)
    a = ci.make_embedder()
    assert ci.make_embedder() is a and len(made) == 1
    assert a.root == embed_model.install_root()
    lb_paths.update_ui_pref("chat_recall_model", "multilingual-e5-small")
    b = ci.make_embedder()
    assert b is not a and a.closed and b.model_id == "multilingual-e5-small"
    ci.release_embedder()
    assert b.closed
    assert ci._EMBEDDER is None


# ---------------------------------------------------------------- チャンク分割と語彙


L = ci.ChunkLimits(600, 800, 80)


def test_chunk_limits():
    assert ci.chunk_limits(embed_model.CATALOG["multilingual-e5-small"]) == ci.ChunkLimits(450, 600, 80)
    assert ci.chunk_limits(embed_model.CATALOG["ruri-v3-30m"]) == ci.ChunkLimits(600, 800, 80)
    assert ci.chunk_limits(None) == ci.ChunkLimits(600, 800, 80)


def test_chunk_short_and_crlf():
    assert ci.chunk_text("hello", L) == ["hello"]
    assert ci.chunk_text("a\r\n\r\nb\rc", L) == ["a\n\nb\nc"]
    assert ci.chunk_text("  \n\n  ", L) == []


def test_chunk_packs_paragraphs():
    paras = [f"{i}" * 200 for i in range(5)]
    chunks = ci.chunk_text("\n\n".join(paras), L)
    assert all(len(c) <= 600 for c in chunks)
    assert chunks[0] == paras[0] + "\n\n" + paras[1]
    assert "\n\n".join(chunks) == "\n\n".join(paras)


def test_chunk_mid_size_paragraph_alone():
    p = "y" * 700
    assert ci.chunk_text("short\n\n" + p + "\n\nafter", L) == ["short", p, "after"]


def test_chunk_hard_split_words():
    words = [f"w{i:04d}" for i in range(400)]          # 2399 文字、空白あり
    p = " ".join(words)
    chunks = ci.chunk_text(p, L)
    assert len(chunks) > 3
    assert all(len(c) <= 600 for c in chunks)
    joined = " ".join(chunks)
    assert all(w in joined for w in words)
    # 切れ目は空白へ戻すので、語の途中では切れない
    assert all(c.split()[0] in words and c.split()[-1] in words for c in chunks)


def test_chunk_hard_split_cjk_overlap_and_terminates():
    p = "あいうえおかきくけこ" * 200                        # 空白の無い 2000 文字
    chunks = ci.chunk_text(p, L)
    assert [len(c) for c in chunks[:-1]] == [600] * (len(chunks) - 1)
    for a, b in itertools.pairwise(chunks):
        assert a[-80:] == b[:80]
    assert chunks[-1] == p[-len(chunks[-1]):]
    assert ci.chunk_text(p, L) == chunks                  # 決定的


def test_chunk_e5_limits():
    p = "x " * 1000
    chunks = ci.chunk_text(p, ci.chunk_limits(embed_model.CATALOG["multilingual-e5-small"]))
    assert all(len(c) <= 450 for c in chunks)


def test_lexical_terms():
    assert ci.lexical_terms("ＺＸ＿９９８１ Baseline") == [("sub", ("zx_9981",)), ("sub", ("baseline",))]
    assert ci.lexical_terms("ピーク位置") == [("grams", ("ピー", "ーク", "ク位", "位置"))]
    assert ci.lexical_terms("値") == [("grams", ("値",))]
    assert ci.lexical_terms("peak-2") == [("sub", ("peak-2",))]
    assert ci.lexical_terms("   ") == []


def test_lexical_score():
    t = ci.normalize("ZX_9981 のピーク位置を補正")
    assert ci.lexical_score(t, ci.lexical_terms("zx_9981")) == 1.0
    assert ci.lexical_score(t, ci.lexical_terms("ピーク位置")) == 1.0
    assert ci.lexical_score(t, ci.lexical_terms("ピーク高さ")) == pytest.approx(2 / 4)
    assert ci.lexical_score(t, ci.lexical_terms("zx_9981 nothing")) == 0.5
    assert ci.lexical_score(t, []) == 0.0


# ---------------------------------------------------------------- 更新


def test_first_update_embeds_all_second_none(tmp_path, emb, idx):
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", "peak offset question"), ("assistant", "use baseline correction")], sid=SID_A)
    _chat(d, [("user", "another chat")], sid=SID_B)
    r = idx.update()
    assert r.ran and not r.cancelled
    [dr] = r.datasets
    assert (dr.name, dr.state, dr.sessions, dr.chunks, dr.embedded) == ("dsA", "updated", 2, 3, 3)
    assert sorted(emb.calls) == sorted(["peak offset question", "use baseline correction",
                                        "another chat"])
    m = _manifest("dsA")
    assert m["n_chunks"] == 3 and m["model"] == "fake-8" and m["dims"] == 8
    assert m["previous"] is None and set(m["files"]) == {SID_A, SID_B}
    mpath = _shard("dsA") / "manifest.json"
    before = mpath.stat().st_mtime_ns
    emb.calls.clear()
    r2 = idx.update()
    assert r2.datasets[0].state == "unchanged" and emb.calls == []
    assert mpath.stat().st_mtime_ns == before
    meta = json.loads((lb_paths.chat_index_dir() / "fake-8" / "meta.json").read_text(encoding="utf-8"))
    assert meta["chunker"] == ci.CHUNKER_VERSION and meta["recipe"] is None


def test_append_message_embeds_only_new_chunk(tmp_path, emb, idx):
    d = _register(tmp_path, "dsA")
    s = _chat(d, [("user", "first"), ("assistant", "second")], sid=SID_A)
    idx.update()
    emb.calls.clear()
    s.messages.append(Message(role="user", content="third"))
    chat_store.write_session_file(d / "_work", s)
    _bump(_cs(d) / f"{SID_A}.json")
    dr = idx.update().datasets[0]
    assert emb.calls == ["third"]
    assert (dr.state, dr.chunks, dr.embedded, dr.reused) == ("updated", 3, 1, 2)
    assert _manifest("dsA")["previous"] is not None


def test_stat_only_change_rewrites_manifest_only(tmp_path, emb, idx, monkeypatch):
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", "hello")], sid=SID_A)
    idx.update()
    m1 = _manifest("dsA")
    _bump(_cs(d) / f"{SID_A}.json")
    emb.calls.clear()
    writes = []
    real = cpaths.atomic_write_bytes
    monkeypatch.setattr(cpaths, "atomic_write_bytes", lambda p, b: (writes.append(p), real(p, b)))
    dr = idx.update().datasets[0]
    assert emb.calls == [] and writes == []
    m2 = _manifest("dsA")
    assert (m2["rows"], m2["vectors"]) == (m1["rows"], m1["vectors"])
    assert m2["files"] != m1["files"] and dr.state == "updated"


def test_delete_chat_and_all_chats(tmp_path, idx):
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", "one")], sid=SID_A)
    _chat(d, [("user", "two")], sid=SID_B)
    idx.update()
    chat_store.delete_session_file(d / "_work", SID_B)
    dr = idx.update().datasets[0]
    assert dr.chunks == 1 and set(_manifest("dsA")["files"]) == {SID_A}
    chat_store.delete_session_file(d / "_work", SID_A)
    dr = idx.update().datasets[0]
    assert dr.state == "removed" and not _shard("dsA").exists()


def test_excluded_sessions(tmp_path, idx):
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", "search tab text")], sid=SID_A, kind="search")
    _chat(d, [("user", "bad id")], sid="NOT-HEX-" + "x" * 8)
    s = _chat(d, [("user", "mismatched")], sid=SID_B)
    os.replace(_cs(d) / f"{SID_B}.json", _cs(d) / f"{SID_C}.json")   # 中身の id とファイル名が違う
    os.replace(_cs(d) / f"{SID_B}.json.bak", _cs(d) / f"{SID_C}.json.bak")
    _chat(d, [("user", "kept")], sid=SID_A[:8] + "1" * 24)
    dr = idx.update().datasets[0]
    files = _manifest("dsA")["files"]
    assert files[SID_A]["indexed"] is False
    assert files["NOT-HEX-" + "x" * 8]["indexed"] is False
    assert files[SID_C]["indexed"] is False and s.id == SID_B
    assert dr.chunks == 1 and dr.sessions == 1


def test_search_tab_only_dataset_keeps_empty_manifest(tmp_path, emb, idx):
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", "q")], sid=SID_A, kind="search")
    assert idx.update().datasets[0].state == "updated"
    assert _manifest("dsA")["n_chunks"] == 0
    assert idx.update().datasets[0].state == "unchanged"
    assert idx.stale_summary() == {}


def test_archived_is_kept_and_filtered(tmp_path, emb, idx):
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", "archived words")], sid=SID_A, archived=True)
    _chat(d, [("user", "live words")], sid=SID_B)
    idx.update()
    hits, _w = idx.search("words")
    assert {h.sid for h in hits} == {SID_B}
    hits, _w = idx.search("words", include_archived=True)
    assert {h.sid for h in hits} == {SID_A, SID_B}
    assert next(h for h in hits if h.sid == SID_A).archived is True


def test_bak_only_session_and_bak_change(tmp_path, emb, idx):
    d = _register(tmp_path, "dsA")
    s = _chat(d, [("user", "from bak")], sid=SID_A)
    (_cs(d) / f"{SID_A}.json").unlink()
    dr = idx.update().datasets[0]
    assert dr.chunks == 1 and _manifest("dsA")["files"][SID_A]["json"] is None
    # .bak だけが更新されても変更として拾う
    s.messages.append(Message(role="assistant", content="bak grew"))
    bak = _cs(d) / f"{SID_A}.json.bak"
    bak.write_text(json.dumps(chat_store.session_to_dict(s)), encoding="utf-8")
    _bump(bak)
    emb.calls.clear()
    dr = idx.update().datasets[0]
    assert emb.calls == ["bak grew"] and dr.chunks == 2
    assert idx.stale_summary() == {}


def test_unreadable_keeps_rows_and_rereads(tmp_path, emb, idx, monkeypatch):
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", "stable text")], sid=SID_A)
    idx.update()
    _bump(_cs(d) / f"{SID_A}.json")
    real = chat_store.read_session_file
    monkeypatch.setattr(chat_store, "read_session_file", lambda p: None)
    r = idx.update()
    dr = r.datasets[0]
    assert dr.unreadable == 1 and dr.chunks == 1
    assert any(w.code == "unreadable" and w.dataset == "dsA" for w in r.warnings())
    entry = _manifest("dsA")["files"][SID_A]
    assert entry["unreadable"] is True and entry["indexed"] is True
    assert idx.stale_summary() == {}                       # stat が同じなら遅れに数えない
    monkeypatch.setattr(chat_store, "read_session_file", real)
    emb.calls.clear()
    dr = idx.update().datasets[0]                          # stat は同じでも読み直す
    assert dr.unreadable == 0 and emb.calls == []
    assert _manifest("dsA")["files"][SID_A]["unreadable"] is False


def test_unreadable_new_file_enters_later(tmp_path, emb, idx, monkeypatch):
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", "late arrival")], sid=SID_A)
    real = chat_store.read_session_file
    monkeypatch.setattr(chat_store, "read_session_file", lambda p: None)
    assert idx.update().datasets[0].chunks == 0
    monkeypatch.setattr(chat_store, "read_session_file", real)
    assert idx.update().datasets[0].chunks == 1
    assert emb.calls == ["late arrival"]


def test_identical_chunk_text_reuses_vector(tmp_path, emb, idx):
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", "same text")], sid=SID_A)
    idx.update()
    emb.calls.clear()
    _chat(d, [("user", "same text"), ("assistant", "new text")], sid=SID_B)
    dr = idx.update().datasets[0]
    assert emb.calls == ["new text"] and dr.reused == 2


def test_model_change_new_slug_old_removed(tmp_path, idx):
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", "x")], sid=SID_A)
    idx.update()
    assert (lb_paths.chat_index_dir() / "fake-8").is_dir()
    other = ci.ChatIndex(FakeEmbedder(dims=16, model_id="fake-16"))
    r = other.update()
    assert r.removed_slugs == 1
    assert not (lb_paths.chat_index_dir() / "fake-8").exists()
    assert _manifest("dsA", "fake-16")["dims"] == 16


@pytest.mark.parametrize("change", [{"chunker": 999}, {"recipe": "old"}, {"dims": 4}])
def test_meta_mismatch_rebuilds(tmp_path, emb, idx, change):
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", "x")], sid=SID_A)
    idx.update()
    mp = lb_paths.chat_index_dir() / "fake-8" / "meta.json"
    meta = json.loads(mp.read_text(encoding="utf-8"))
    mp.write_text(json.dumps({**meta, **change}), encoding="utf-8")
    emb.calls.clear()
    idx.update()
    assert emb.calls == ["x"]
    assert json.loads(mp.read_text(encoding="utf-8"))["chunker"] == ci.CHUNKER_VERSION


def _corrupt_manifest(name, how):
    mp = _shard(name) / "manifest.json"
    m = json.loads(mp.read_text(encoding="utf-8"))
    if how == "nonjson":
        mp.write_text("{not json", encoding="utf-8")
        return
    if how == "empty":
        mp.write_bytes(b"")
        return
    if how == "version":
        m["version"] = 2
    elif how == "model":
        m["model"] = "other"
    elif how == "entry":
        m["files"][SID_A] = {"json": None, "bak": None, "fingerprint": None,
                             "indexed": True, "unreadable": False}
    elif how == "entry-type":
        m["files"][SID_A]["indexed"] = 1
    elif how == "previous":
        m["previous"] = {"rows": "../escape.json", "vectors": "vectors-0000000000000000.npy"}
    mp.write_text(json.dumps(m), encoding="utf-8")


@pytest.mark.parametrize("how", ["nonjson", "empty", "version", "model", "entry", "entry-type",
                                 "previous"])
def test_invalid_manifest_is_empty_and_rebuilt(tmp_path, emb, idx, how):
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", "needle text")], sid=SID_A)
    idx.update()
    _corrupt_manifest("dsA", how)
    hits, warnings = idx.search("needle")
    assert hits == [] and [w.code for w in warnings] == ["shard-invalid"]
    emb.calls.clear()
    idx.update()
    assert emb.calls == ["needle text"]
    assert idx.search("needle")[0]


def _vec_path(name):
    return _shard(name) / _manifest(name)["vectors"]


def _rows_path(name):
    return _shard(name) / _manifest(name)["rows"]


@pytest.mark.parametrize("how", ["zero", "rows", "nan"])
def test_broken_vectors_are_rebuilt(tmp_path, emb, idx, how):
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", "needle one"), ("assistant", "needle two")], sid=SID_A)
    idx.update()
    vp = _vec_path("dsA")
    size = vp.stat().st_size
    if how == "zero":
        vp.write_bytes(b"")
    elif how == "rows":
        buf = io.BytesIO()
        np.save(buf, np.zeros((1, 8), dtype=np.float16), allow_pickle=False)
        vp.write_bytes(buf.getvalue())
    else:                                           # 大きさは同じで非有限値（同名のまま直す）
        buf = io.BytesIO()
        np.save(buf, np.full((2, 8), np.nan, dtype=np.float16), allow_pickle=False)
        assert len(buf.getvalue()) == size
        vp.write_bytes(buf.getvalue())
    ci._SHARD_CACHE.clear()
    assert idx.search("needle")[0] == []
    emb.calls.clear()
    idx.update()
    assert sorted(emb.calls) == ["needle one", "needle two"]
    hits, warnings = idx.search("needle")
    assert len(hits) == 2 and warnings == []


def test_broken_current_with_previous_is_rebuilt(tmp_path, emb, idx):
    d = _register(tmp_path, "dsA")
    s = _chat(d, [("user", "needle one")], sid=SID_A)
    idx.update()
    s.messages.append(Message(role="assistant", content="needle two"))
    chat_store.write_session_file(d / "_work", s)
    _bump(_cs(d) / f"{SID_A}.json")
    idx.update()
    m = _manifest("dsA")
    assert m["previous"] is not None
    (_shard("dsA") / m["vectors"]).write_bytes(b"")
    ci._SHARD_CACHE.clear()
    # 検索は previous から読める
    hits, _w = idx.search("needle")
    assert [h.idx for h in hits] == [1]
    emb.calls.clear()
    dr = idx.update().datasets[0]
    assert dr.state == "updated"
    assert emb.calls == ["needle two"]                     # previous の本文は再利用
    hits, warnings = idx.search("needle")
    assert len(hits) == 2 and warnings == []
    assert (_shard("dsA") / _manifest("dsA")["vectors"]).stat().st_size > 0


def test_work_dir_change_rereads(tmp_path, emb, idx, monkeypatch):
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", "moved")], sid=SID_A)
    idx.update()
    shutil.copytree(d / "_work", d / "_work2")             # stat も同じ
    (d / "myanalysis.toml").write_text('work_dir = "_work2"\n', encoding="utf-8")
    reads = []
    real = chat_store.read_session_file
    monkeypatch.setattr(chat_store, "read_session_file", lambda p: (reads.append(p), real(p))[1])
    emb.calls.clear()
    dr = idx.update().datasets[0]
    assert reads and dr.state == "updated" and emb.calls == []
    assert _manifest("dsA")["work_dir"] == str(d / "_work2")


def test_unmounted_and_no_host_keep_shard(tmp_path, idx):
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", "x")], sid=SID_A)
    idx.update()
    shutil.rmtree(d)
    r = idx.update()
    assert (r.datasets[0].state, r.datasets[0].reason) == ("skipped", "unreachable")
    assert [w.code for w in r.warnings()] == ["unreachable"]
    assert _shard("dsA").is_dir()
    config.unregister_dataset("dsA")
    config.register_dataset("dsA", str(d), host="OTHERPC")
    r = idx.update()
    assert (r.datasets[0].state, r.datasets[0].reason) == ("skipped", "no-host")
    assert r.warnings() == [] and _shard("dsA").is_dir()


def test_missing_work_dir_keeps_shard(tmp_path, idx):
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", "x")], sid=SID_A)
    idx.update()
    shutil.rmtree(d / "_work")
    dr = idx.update().datasets[0]
    assert (dr.state, dr.reason) == ("skipped", "unreachable") and _shard("dsA").is_dir()


def test_unregistered_dataset_shard_removed(tmp_path, idx):
    a = _register(tmp_path, "dsA")
    b = _register(tmp_path, "dsB")
    _chat(a, [("user", "a")], sid=SID_A)
    _chat(b, [("user", "b")], sid=SID_B)
    idx.update()
    config.unregister_dataset("dsB")
    r = idx.update()
    assert r.removed_shards == 1 and not _shard("dsB").exists() and _shard("dsA").exists()


def test_locked_update_writes_nothing_and_recall_still_searches(tmp_path, emb, idx):
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", "needle")], sid=SID_A)
    idx.update()
    _chat(d, [("user", "needle later")], sid=SID_B)
    lock = lb_paths.chat_index_dir() / "fake-8.lock"
    emb.calls.clear()
    with filelock.exclusive_lock(lock):
        r = idx.update(blocking=False)
        assert not r.ran and [x.reason for x in r.datasets] == ["locked"]
        assert [w.code for w in r.warnings()] == ["locked"]
        res = idx.recall("needle", update=True, blocking=False)
    assert emb.calls == []
    assert res.updated_index is False and [h.sid for h in res.hits] == [SID_A]
    assert res.stale == {"dsA": 1}
    assert any(w.code == "locked" for w in res.warnings)


def test_first_nonblocking_update_creates_dir(tmp_path, idx):
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", "x")], sid=SID_A)
    assert not lb_paths.chat_index_dir().exists()
    assert idx.update(blocking=False).ran


def test_blocking_lock_timeout_is_index_locked(tmp_path, idx, monkeypatch):
    _register(tmp_path, "dsA")

    def timeout(path):
        raise filelock.LockTimeout(36, "lock timeout")

    monkeypatch.setattr(filelock, "exclusive_lock", timeout)
    with pytest.raises(ci.IndexLocked):
        idx.update()


def test_rebuild_reembeds_without_deadlock(tmp_path, emb, idx):
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", "x"), ("assistant", "y")], sid=SID_A)
    idx.update()
    emb.calls.clear()
    r = idx.rebuild()
    assert r.ran and sorted(emb.calls) == ["x", "y"]
    emb.calls.clear()
    idx.rebuild(["dsA"])
    assert sorted(emb.calls) == ["x", "y"]


def test_cancel_writes_nothing(tmp_path, emb, idx):
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", f"t{i}") for i in range(10)], sid=SID_A)
    r = idx.update(cancel=lambda: True)
    assert r.cancelled and r.datasets[0].reason == "cancelled"
    assert not _shard("dsA").exists()
    assert any(w.code == "cancelled" for w in r.warnings())


def test_cancel_during_last_batch_writes_nothing(tmp_path, emb, idx):
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", "only")], sid=SID_A)
    flag = [False]
    emb.after_batch = lambda: flag.__setitem__(0, True)
    r = idx.update(cancel=lambda: flag[0])
    assert emb.calls == ["only"]
    assert r.cancelled and not _shard("dsA").exists()


def test_budget_publishes_finished_sessions(tmp_path, emb, idx, monkeypatch):
    d = _register(tmp_path, "dsA")
    _register(tmp_path, "dsB")
    _chat(d, [("user", "old 1"), ("user", "old 2")], sid=SID_A, updated=100.0)
    _chat(d, [("user", "mid 1"), ("user", "mid 2")], sid=SID_B, updated=200.0)
    _chat(d, [("user", "new 1"), ("user", "new 2")], sid=SID_C, updated=300.0)
    clock = [0.0]
    monkeypatch.setattr(ci, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(ci, "_EMBED_GROUP", 2)
    emb.after_batch = lambda: clock.__setitem__(0, clock[0] + 1.0)
    r = idx.update(budget_s=1.5)
    dr = r.datasets[0]
    assert (dr.state, dr.reason, dr.partial) == ("updated", "budget", True)
    assert r.datasets[1].reason == "budget"                # 残りのデータセットは次回
    assert [w.code for w in r.warnings()] == ["budget"]
    files = _manifest("dsA")["files"]
    assert set(files) == {SID_B, SID_C}                     # 新しいチャットから入る
    assert idx.stale_summary(["dsA"]) == {"dsA": 1}
    first = list(emb.calls)
    monkeypatch.setattr(ci, "_monotonic", lambda: 0.0)
    emb.after_batch = None
    dr = idx.update().datasets[0]
    assert dr.partial is False and dr.chunks == 6
    assert sorted(first + emb.calls[len(first):]) == sorted(
        ["old 1", "old 2", "mid 1", "mid 2", "new 1", "new 2"])


def test_budget_shorter_than_one_group_still_progresses(tmp_path, emb, idx, monkeypatch):
    # 1 セッションの埋め込みが予算より長くても、1 回の更新で少なくとも 1 セッションは公開する
    d = _register(tmp_path, "dsA")
    sids = [f"{i:08x}" + "3" * 24 for i in range(3)]
    for i, sid in enumerate(sids):
        _chat(d, [("user", f"s{i} text {j}") for j in range(70)], sid=sid, updated=100.0 + i)
    clock = [0.0]
    monkeypatch.setattr(ci, "_monotonic", lambda: clock[0])
    emb.after_batch = lambda: clock.__setitem__(0, clock[0] + 1.0)   # 4 件ごとに 1 秒
    published = []
    for _run in range(3):
        dr = idx.update(budget_s=5).datasets[0]
        published.append(dr.sessions)
    assert published == [1, 2, 3]
    assert idx.stale_summary() == {}
    assert len(emb.calls) == 210                            # 捨てて埋め込み直した分は無い


def test_budget_validation(idx):
    for bad in (0, -1, float("nan"), float("inf"), True):
        with pytest.raises(ValueError):
            idx.update(budget_s=bad)


def test_gc_keeps_current_and_previous(tmp_path, idx):
    d = _register(tmp_path, "dsA")
    s = _chat(d, [("user", "v1")], sid=SID_A)
    idx.update()
    for i in range(2, 5):
        s.messages.append(Message(role="user", content=f"v{i}"))
        chat_store.write_session_file(d / "_work", s)
        _bump(_cs(d) / f"{SID_A}.json", ns=2_000_000_000 * i)
    (_shard("dsA") / "rows-0000000000000000.json").write_text("{}", encoding="utf-8")
    (_shard("dsA") / "manifest.json.123.tmp").write_text("x", encoding="utf-8")
    idx.update()
    m = _manifest("dsA")
    names = sorted(p.name for p in _shard("dsA").iterdir())
    assert names == sorted(["manifest.json", m["rows"], m["vectors"], m["previous"]["rows"],
                            m["previous"]["vectors"]])


def test_manifest_write_failure_skips_gc(tmp_path, idx, monkeypatch):
    d = _register(tmp_path, "dsA")
    s = _chat(d, [("user", "v1")], sid=SID_A)
    idx.update()
    old = _manifest("dsA")
    s.messages.append(Message(role="user", content="v2"))
    chat_store.write_session_file(d / "_work", s)
    _bump(_cs(d) / f"{SID_A}.json")
    real = cpaths.atomic_write_text

    def fail_manifest(p, text, **kw):
        if p.name == "manifest.json":
            raise cpaths.MountWriteError(p, attempts=5, detail="boom")
        return real(p, text, **kw)

    monkeypatch.setattr(cpaths, "atomic_write_text", fail_manifest)
    r = idx.update()
    assert (r.datasets[0].state, r.datasets[0].reason) == ("skipped", "write")
    assert [w.code for w in r.warnings()] == ["write"]
    assert _manifest("dsA") == old
    assert (_shard("dsA") / old["rows"]).exists() and (_shard("dsA") / old["vectors"]).exists()


def test_reader_rereads_manifest_when_both_pairs_vanish(tmp_path, idx, monkeypatch):
    d = _register(tmp_path, "dsA")
    s = _chat(d, [("user", "needle one")], sid=SID_A)
    idx.update()
    stale = _manifest("dsA")
    s.messages.append(Message(role="user", content="needle two"))
    chat_store.write_session_file(d / "_work", s)
    _bump(_cs(d) / f"{SID_A}.json")
    idx.update()
    s.messages.append(Message(role="user", content="needle three"))
    chat_store.write_session_file(d / "_work", s)
    _bump(_cs(d) / f"{SID_A}.json", ns=4_000_000_000)
    idx.update()
    assert not (_shard("dsA") / stale["rows"]).exists()     # 2 回の更新で消えた
    real = ci._read_manifest
    seen = []

    def first_stale(shard_dir, dataset, spec):
        seen.append(1)
        if len(seen) == 1:
            return stale
        return real(shard_dir, dataset, spec)

    monkeypatch.setattr(ci, "_read_manifest", first_stale)
    ci._SHARD_CACHE.clear()
    hits, warnings = idx.search("needle")
    assert len(hits) == 3 and warnings == [] and len(seen) == 2


def test_reload_false_does_not_reload(tmp_path, idx, monkeypatch):
    _register(tmp_path, "dsA")
    called = []
    monkeypatch.setattr(config, "reload_datasets", lambda *a, **k: called.append(1))
    idx.update(reload=False)
    assert called == []
    idx.update()
    assert called == [1]


def test_progress_reports_embedding(tmp_path, idx):
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", f"t{i}") for i in range(6)], sid=SID_A)
    seen = []
    idx.update(progress=lambda ds, done, total: seen.append((ds, done, total)))
    assert seen[-1] == ("dsA", 6, 6)


# ---------------------------------------------------------------- 検索


def test_identifier_query_is_found_even_if_vector_is_far(tmp_path, emb, idx):
    d = _register(tmp_path, "dsA")
    target = "calibration constant zx_9981_cal was wrong"
    _chat(d, [("user", target)], sid=SID_A)
    for i in range(30):
        _chat(d, [("user", f"unrelated chat number {i} about plots")],
              sid=f"{i:08x}" + "1" * 24)
    idx.update()
    emb.query_vec = -emb.vec(target)                       # ベクトルは一番遠い
    hits, _w = idx.search("zx_9981_cal", k=5)
    assert SID_A in [h.sid for h in hits[:3]]
    assert hits[0].lexical == 1.0


def test_lexical_ties_are_ordered_by_cosine_not_dataset_order(tmp_path, emb, idx):
    a = _register(tmp_path, "dsA")
    b = _register(tmp_path, "dsB")
    _chat(a, [("user", "token zx_1 in A")], sid=SID_A)
    _chat(b, [("user", "token zx_1 in B")], sid=SID_B)
    idx.update()
    emb.query_vec = emb.vec("token zx_1 in B")
    hits, _w = idx.search("zx_1")
    assert [h.dataset for h in hits[:2]] == ["dsB", "dsA"]
    assert hits[0].score > hits[1].score


def test_long_message_folds_to_one_hit(tmp_path, idx):
    d = _register(tmp_path, "dsA")
    long = " ".join(f"needle{i}" for i in range(500))
    _chat(d, [("user", long)], sid=SID_A)
    idx.update()
    assert _manifest("dsA")["n_chunks"] > 3
    hits, _w = idx.search("needle1")
    assert len(hits) == 1 and (hits[0].sid, hits[0].idx) == (SID_A, 1)
    assert len(hits[0].snippet) <= ci.SNIPPET_CHARS


def test_same_sid_in_two_datasets(tmp_path, idx):
    a = _register(tmp_path, "dsA")
    b = _register(tmp_path, "dsB")
    _chat(a, [("user", "shared copy")], sid=SID_A)
    _chat(b, [("user", "shared copy")], sid=SID_A)
    idx.update()
    hits, _w = idx.search("shared")
    assert sorted(h.dataset for h in hits) == ["dsA", "dsB"]


def test_dataset_filter_and_unregistered(tmp_path, idx):
    a = _register(tmp_path, "dsA")
    b = _register(tmp_path, "dsB")
    _chat(a, [("user", "alpha words")], sid=SID_A)
    _chat(b, [("user", "beta words")], sid=SID_B)
    idx.update()
    assert {h.dataset for h in idx.search("words", datasets=["dsB"])[0]} == {"dsB"}
    config.unregister_dataset("dsB")
    config.reload_datasets()
    assert {h.dataset for h in idx.search("words")[0]} == {"dsA"}


def test_k_cap_snippet_and_to_dict(tmp_path, idx):
    d = _register(tmp_path, "dsA")
    for i in range(210):
        _chat(d, [("user", f"item {i}")], sid=f"{i:08x}" + "2" * 24)
    _chat(d, [("user", "x " * 400)], sid=SID_A, title="long one")
    idx.update()
    hits, _w = idx.search("item", k=1000)
    assert len(hits) <= ci.MAX_K
    hits, _w = idx.search("x", k=500)
    h = next(h for h in hits if h.sid == SID_A)
    assert len(h.snippet) <= ci.SNIPPET_CHARS
    dd = h.to_dict()
    assert set(dd) == {"sid", "idx", "dataset", "title", "archived", "role", "score", "cosine",
                       "lexical", "snippet", "updated"}
    assert dd["sid"] == SID_A[:8] and dd["title"] == "long one" and dd["role"] == "user"
    assert len(dd["updated"]) == len("2026-01-01 00:00")


def test_empty_query_does_not_embed(tmp_path, emb, idx):
    assert idx.search("   ") == ([], [])
    assert emb.queries == []


# ---------------------------------------------------------------- 読むだけの API


def test_stale_summary_reads_no_json(tmp_path, idx, monkeypatch):
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", "x")], sid=SID_A)
    _chat(d, [("user", "y")], sid=SID_B)
    assert ci.stale_summary(model_id="fake-8") == {"dsA": 2}
    idx.update()
    monkeypatch.setattr(chat_store, "read_session_file",
                        lambda p: pytest.fail("stale_summary must not read chats"))
    assert ci.stale_summary(model_id="fake-8") == {}
    _bump(_cs(d) / f"{SID_A}.json")
    assert ci.stale_summary(model_id="fake-8") == {"dsA": 1}
    (_cs(d) / f"{SID_B}.json").unlink()
    (_cs(d) / f"{SID_B}.json.bak").unlink()
    assert ci.stale_summary(model_id="fake-8") == {"dsA": 2}


def test_status(tmp_path, idx):
    d = _register(tmp_path, "dsA")
    _register(tmp_path, "dsB")
    _chat(d, [("user", "x"), ("assistant", "y")], sid=SID_A)
    idx.update()
    st = ci.status(model_id="fake-8")
    assert st.ready is False and st.enabled is False and st.model == "fake-8"
    by = {s.name: s for s in st.datasets}
    assert (by["dsA"].sessions, by["dsA"].chunks) == (1, 2) and by["dsA"].bytes > 0
    assert by["dsA"].updated is not None and by["dsB"].updated is None
    assert st.total_chunks == 2 and st.total_bytes == by["dsA"].bytes
    dd = st.to_dict()
    assert dd["datasets"][0]["name"] == "dsA" and isinstance(dd["last_update"], str)


def test_dataset_of_session(tmp_path, idx):
    a = _register(tmp_path, "dsA")
    b = _register(tmp_path, "dsB")
    _chat(a, [("user", "x")], sid=SID_A)
    _chat(b, [("user", "y")], sid="aaaaaaaa" + "f" * 24)
    _chat(b, [("user", "z")], sid=SID_B)
    idx.update()
    assert ci.dataset_of_session(SID_B[:8], model_id="fake-8") == "dsB"
    assert ci.dataset_of_session("aaaaaaaa", model_id="fake-8") is None       # 曖昧
    assert ci.dataset_of_session(SID_A[:12], model_id="fake-8") == "dsA"
    assert ci.dataset_of_session("deadbeef", model_id="fake-8") is None
    assert ci.dataset_of_session("xyz", model_id="fake-8") is None


def test_read_message(tmp_path):
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", "hello there"), ("assistant", "answer")], sid=SID_A)
    assert ci.read_message("dsA", SID_A, 1) == "hello there"
    assert ci.read_message("dsA", SID_A, 2) == "answer"
    assert ci.read_message("dsA", SID_A, 9) is None
    assert ci.read_message("dsA", "../x", 1) is None
    assert ci.read_message("dsA", SID_A, -1) is None
    assert ci.read_message("nope", SID_A, 1) is None


def test_clear(tmp_path, idx):
    a = _register(tmp_path, "dsA")
    b = _register(tmp_path, "dsB")
    _chat(a, [("user", "x")], sid=SID_A)
    _chat(b, [("user", "y")], sid=SID_B)
    idx.update()
    assert idx.clear(["dsA"]) == 1 and not _shard("dsA").exists() and _shard("dsB").exists()
    assert ci.clear() == 1
    assert not (lb_paths.chat_index_dir() / "fake-8").exists()
    assert ci.clear() == 0


def test_clear_nonblocking_keeps_locked_slug_and_raises(tmp_path, idx):
    a = _register(tmp_path, "dsA")
    _chat(a, [("user", "x")], sid=SID_A)
    idx.update()
    other = lb_paths.chat_index_dir() / "other-1"
    (other / ci.dskey("dsA")).mkdir(parents=True)
    with filelock.exclusive_lock(lb_paths.chat_index_dir() / "fake-8.lock"):
        with pytest.raises(ci.IndexLocked, match="fake-8"):
            ci.clear(blocking=False)
        assert _shard("dsA").exists() and not other.exists()      # 取れた slug だけ消す
        with pytest.raises(ci.IndexLocked):
            ci.clear(["dsA"], model_id="fake-8", blocking=False)
        assert _shard("dsA").exists()
    assert ci.clear(blocking=False) == 1 and not _shard("dsA").exists()


def test_recall_combines(tmp_path, emb, idx):
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", "needle")], sid=SID_A)
    res = idx.recall("needle", update=True)
    assert res.updated_index and [h.sid for h in res.hits] == [SID_A] and res.stale == {}
    res = idx.recall("needle")
    assert res.updated_index is False and emb.calls == ["needle"]


def test_recall_reload_false_does_not_reload(tmp_path, idx, monkeypatch):
    """GUI の裏のスレッド（Ctrl+F の埋め込み検索。Issue #128）は登録簿を読み直さない。"""
    d = _register(tmp_path, "dsA")
    _chat(d, [("user", "needle")], sid=SID_A)
    called = []
    monkeypatch.setattr(config, "reload_datasets", lambda *a, **k: called.append(1))
    res = idx.recall("needle", update=True, reload=False)
    assert called == [] and res.updated_index and [h.sid for h in res.hits] == [SID_A]
    idx.recall("needle", update=True)
    assert called == [1]


def test_invalid_slug_rejected():
    with pytest.raises(ValueError):
        ci.ChatIndex(types.SimpleNamespace(model_id="../x", dims=8))
