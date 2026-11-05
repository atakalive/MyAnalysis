"""common/embed_model.py — 埋め込みモデルの catalog・導入・削除・依存・ONNX 推論（Issue #125）。

ネット・pip・onnxruntime・tokenizers は使わない（取得は ``_urlopen``、pip は ``_popen``、
依存は ``_find_spec`` と ``_import_deps`` の差し替え）。導入先は conftest が tmp に向けている。
実モデルの推論は ``MYANALYSIS_TEST_EMBED_MODEL=1`` で依存とモデルが入っているときだけ。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import threading
import time
import types
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from common import embed_model as em
from common import self_update

# ---------------------------------------------------------------- 固定値


def test_catalog_constants_match_issue():
    assert em.DEFAULT_MODEL == "ruri-v3-30m"
    assert set(em.CATALOG) == {"ruri-v3-30m", "multilingual-e5-small"}
    assert em.REQUIRED_PACKAGES == (("onnxruntime", "onnxruntime"), ("tokenizers", "tokenizers"))
    ruri = em.CATALOG["ruri-v3-30m"]
    assert (ruri.dims, ruri.max_tokens, ruri.pooling) == (256, 1024, "mean")
    assert (ruri.query_prefix, ruri.document_prefix) == ("検索クエリ: ", "検索文書: ")
    assert ruri.license == "Apache-2.0"
    e5 = em.CATALOG["multilingual-e5-small"]
    assert (e5.dims, e5.max_tokens, e5.pooling) == (384, 512, "mean")
    assert (e5.query_prefix, e5.document_prefix) == ("query: ", "passage: ")
    assert e5.license == "MIT"
    for m in em.CATALOG.values():
        assert re.fullmatch(r"chat\.recall\.model\.[a-z0-9_]+", m.label_key)
        assert [f.name for f in m.files] == ["model.onnx", "tokenizer.json"]
        for f in m.files:
            assert re.search(r"/resolve/[0-9a-f]{40}/", f.url), f.url
            assert re.fullmatch(r"[0-9a-f]{64}", f.sha256)


def test_pinned_files():
    pins = {(m.id, f.name): (f.sha256, f.size) for m in em.CATALOG.values() for f in m.files}
    assert pins == {
        ("ruri-v3-30m", "model.onnx"):
            ("5ffd75c4d39c2d783efc46f45c1b5c9b0e19699e2b2b60396ae45dbd7d8ef869", 147_020_821),
        ("ruri-v3-30m", "tokenizer.json"):
            ("0a94ac9a0a02c067bdef25b72ae9f4ee33f48f552e55988d444f6d25eeb1d062", 6_724_873),
        ("multilingual-e5-small", "model.onnx"):
            ("ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665", 470_268_510),
        ("multilingual-e5-small", "tokenizer.json"):
            ("0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39", 17_082_730),
    }
    ruri = em.CATALOG["ruri-v3-30m"]
    assert "/onnx-community/ruri-v3-30m-ONNX/resolve/1b41a27f31213fc55a9c4653c067770e39406408/onnx/model.onnx" \
        in ruri.files[0].url
    assert "/cl-nagoya/ruri-v3-30m/resolve/24899e5de370b56d179604a007c0d727bf144504/tokenizer.json" \
        in ruri.files[1].url


def test_recipe_tracks_document_side_only():
    m = em.CATALOG["ruri-v3-30m"]
    assert m.recipe == em.CATALOG["ruri-v3-30m"].recipe
    assert re.fullmatch(r"[0-9a-f]{64}", m.recipe)
    assert replace(m, document_prefix="x: ").recipe != m.recipe
    assert replace(m, max_tokens=512).recipe != m.recipe
    assert replace(m, files=(replace(m.files[0], sha256="0" * 64), m.files[1])).recipe != m.recipe
    assert replace(m, query_prefix="q: ").recipe == m.recipe
    assert replace(m, label_key="x.y").recipe == m.recipe


# ---------------------------------------------------------------- 偽の取得


MODEL_BLOB = b"fake onnx model " * 4000          # 64 KiB を超える（複数回の read）
TOK_BLOB = b'{"fake": "tokenizer"}'


def _make_model(model_id="test-tiny", model_blob=MODEL_BLOB, tok_blob=TOK_BLOB, dims=8,
                max_tokens=64) -> em.EmbedModel:
    base = em.CATALOG["ruri-v3-30m"]
    return replace(
        base, id=model_id, dims=dims, max_tokens=max_tokens,
        files=(em.ModelFile(f"https://example.invalid/{model_id}/model.onnx",
                            hashlib.sha256(model_blob).hexdigest(), len(model_blob), "model.onnx"),
               em.ModelFile(f"https://example.invalid/{model_id}/tokenizer.json",
                            hashlib.sha256(tok_blob).hexdigest(), len(tok_blob), "tokenizer.json")))


class _Resp:
    def __init__(self, data: bytes, fail_after: int | None = None):
        self._f = io.BytesIO(data)
        self._fail_after = fail_after
        self._reads = 0

    def read(self, n):
        self._reads += 1
        if self._fail_after is not None and self._reads > self._fail_after:
            raise OSError("connection reset")
        return self._f.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def net(monkeypatch):
    """テスト用モデルを catalog に足し、取得を偽物にする。blobs を書き換えると中身が変わる。"""
    model = _make_model()
    monkeypatch.setitem(em.CATALOG, model.id, model)
    state = {"calls": [], "blobs": {model.files[0].url: MODEL_BLOB, model.files[1].url: TOK_BLOB},
             "fail_open": None, "fail_after": None, "model": model}

    def urlopen(url, timeout):
        state["calls"].append(url)
        if state["fail_open"] is not None:
            raise state["fail_open"]
        return _Resp(state["blobs"][url], state["fail_after"])

    monkeypatch.setattr(em, "_urlopen", urlopen)
    return state


def _never():
    return False


def _root() -> Path:
    return em.install_root()


def _tmpdirs():
    return sorted(p.name for p in _root().iterdir() if ".tmp-" in p.name) if _root().exists() else []


# ---------------------------------------------------------------- 導入


def test_install_and_status(net):
    assert em.status("test-tiny").state == "missing"
    seen = []
    r = em.install("test-tiny", lambda d, t: seen.append((d, t)), _never)
    assert r.ok, r
    st = em.status("test-tiny")
    assert st.state == "installed" and st.size == len(MODEL_BLOB) + len(TOK_BLOB)
    d = _root() / "test-tiny"
    assert (d / "model.onnx").read_bytes() == MODEL_BLOB
    assert (d / "tokenizer.json").read_bytes() == TOK_BLOB
    info = json.loads((d / "installed.json").read_text(encoding="utf-8"))
    assert info["model"] == "test-tiny"
    assert info["files"]["model.onnx"] == {"sha256": hashlib.sha256(MODEL_BLOB).hexdigest(),
                                           "size": len(MODEL_BLOB)}
    assert _tmpdirs() == []
    total = len(MODEL_BLOB) + len(TOK_BLOB)
    assert all(t == total for _d, t in seen)
    assert [d for d, _t in seen] == sorted(d for d, _t in seen)
    assert seen[-1][0] == total


def test_install_when_installed_does_not_fetch(net):
    assert em.install("test-tiny", lambda d, t: None, _never).ok
    net["calls"].clear()
    assert em.install("test-tiny", lambda d, t: None, _never).ok
    assert net["calls"] == []


def test_install_replaces_broken(net):
    d = _root() / "test-tiny"
    d.mkdir(parents=True)
    (d / "model.onnx").write_bytes(b"partial")
    assert em.status("test-tiny").state == "broken"
    assert em.install("test-tiny", lambda d, t: None, _never).ok
    assert em.status("test-tiny").state == "installed"
    assert (d / "model.onnx").read_bytes() == MODEL_BLOB
    assert _tmpdirs() == []


@pytest.mark.parametrize("blob", [b"x" * len(MODEL_BLOB), MODEL_BLOB[:-1], MODEL_BLOB + b"!"],
                         ids=["sha256", "short", "long"])
def test_install_integrity(net, blob):
    net["blobs"][net["model"].files[0].url] = blob
    r = em.install("test-tiny", lambda d, t: None, _never)
    assert not r.ok and r.error == "integrity"
    assert em.status("test-tiny").state == "missing"
    assert _tmpdirs() == []


def test_install_network_errors(net):
    net["fail_open"] = OSError("dns failure")
    r = em.install("test-tiny", lambda d, t: None, _never)
    assert (r.ok, r.error) == (False, "network")
    net["fail_open"] = None
    net["fail_after"] = 1
    r = em.install("test-tiny", lambda d, t: None, _never)
    assert (r.ok, r.error) == (False, "network")
    assert em.status("test-tiny").state == "missing" and _tmpdirs() == []


def test_install_io_error(net, monkeypatch):
    real_open = open

    def bad_open(path, mode="r", *a, **k):
        if "b" in mode and "w" in mode and ".tmp-" in str(path):
            raise PermissionError("denied")
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr("builtins.open", bad_open)
    r = em.install("test-tiny", lambda d, t: None, _never)
    assert (r.ok, r.error) == (False, "io")


def test_install_cancelled(net):
    n = [0]

    def cancel_on_second():
        n[0] += 1
        return n[0] > 1

    r = em.install("test-tiny", lambda d, t: None, cancel_on_second)
    assert (r.ok, r.error) == (False, "cancelled")
    assert em.status("test-tiny").state == "missing" and _tmpdirs() == []


def test_install_unknown_model():
    r = em.install("no-such-model", lambda d, t: None, _never)
    assert (r.ok, r.error) == (False, "io")


def test_install_cleans_stale_workdirs(net):
    root = _root()
    old = root / "test-tiny.tmp-old"
    new = root / "test-tiny.tmp-new"
    old.mkdir(parents=True)
    new.mkdir()
    past = time.time() - em.STALE_WORKDIR_S - 60
    os.utime(old, (past, past))
    assert em.install("test-tiny", lambda d, t: None, _never).ok
    assert not old.exists()
    assert new.exists()


# ---------------------------------------------------------------- 状態


def _install_files(root: Path, model: em.EmbedModel, *, info: dict | None = None,
                   model_blob=MODEL_BLOB, tok_blob=TOK_BLOB) -> Path:
    d = root / model.id
    d.mkdir(parents=True, exist_ok=True)
    (d / "model.onnx").write_bytes(model_blob)
    (d / "tokenizer.json").write_bytes(tok_blob)
    data = info if info is not None else em._installed_info(model)
    (d / "installed.json").write_text(json.dumps(data), encoding="utf-8")
    return d


def test_status_broken_variants(monkeypatch):
    model = _make_model()
    monkeypatch.setitem(em.CATALOG, model.id, model)
    root = _root()
    d = _install_files(root, model)
    assert em.status(model.id).state == "installed"
    (d / "installed.json").unlink()
    assert em.status(model.id).state == "broken"
    (d / "installed.json").write_text("{not json", encoding="utf-8")
    assert em.status(model.id).state == "broken"
    _install_files(root, model)
    # catalog の版上げ（sha256 が変わった）
    bumped = replace(model, files=(replace(model.files[0], sha256="0" * 64), model.files[1]))
    monkeypatch.setitem(em.CATALOG, model.id, bumped)
    assert em.status(model.id).state == "broken"
    monkeypatch.setitem(em.CATALOG, model.id, model)
    (d / "model.onnx").write_bytes(MODEL_BLOB[:-1])
    assert em.status(model.id).state == "broken"


# ---------------------------------------------------------------- 削除


def test_uninstall(net):
    assert em.install("test-tiny", lambda d, t: None, _never).ok
    r = em.uninstall("test-tiny")
    assert r.ok, r
    assert em.status("test-tiny").state == "missing"
    assert _tmpdirs() == []


def test_uninstall_without_root_is_ok(net):
    assert not _root().exists()
    assert em.uninstall("test-tiny").ok
    assert not _root().exists()


def test_uninstall_in_use_changes_nothing(net, monkeypatch):
    assert em.install("test-tiny", lambda d, t: None, _never).ok

    def busy(src, dst):
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(em, "_rename", busy)
    r = em.uninstall("test-tiny")
    assert (r.ok, r.error) == (False, "in_use")
    assert em.status("test-tiny").state == "installed"


# ---------------------------------------------------------------- 依存


@pytest.mark.parametrize("present,expected", [
    ({"onnxruntime", "tokenizers"}, []),
    ({"tokenizers"}, ["onnxruntime"]),
    ({"onnxruntime"}, ["tokenizers"]),
    (set(), ["onnxruntime", "tokenizers"]),
])
def test_missing_packages(monkeypatch, present, expected):
    monkeypatch.setattr(em, "_find_spec", lambda n: object() if n in present else None)
    assert em.missing_packages() == expected


def test_missing_packages_find_spec_errors(monkeypatch):
    def boom(name):
        raise ValueError("bad spec")
    monkeypatch.setattr(em, "_find_spec", boom)
    assert em.missing_packages() == ["onnxruntime", "tokenizers"]


def test_pip_install_argv():
    argv = em.pip_install_argv()
    assert argv == [self_update.python_for_pip(), "-m", "pip", "install", "onnxruntime",
                    "tokenizers", "--disable-pip-version-check", "--no-input"]


class _FakeProc:
    def __init__(self, lines, rc=0, hang=False):
        self.stdout = io.StringIO("".join(line + "\n" for line in lines))
        self._rc = rc
        self._hang = hang
        self.returncode = None
        self.terminated = False
        self._done = threading.Event()
        if not hang:
            self.returncode = rc

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def test_install_packages_success(monkeypatch):
    calls = []

    def popen(argv, **kw):
        calls.append((argv, kw))
        return _FakeProc(["Collecting onnxruntime", "Successfully installed onnxruntime tokenizers"])

    monkeypatch.setattr(em, "_popen", popen)
    lines = []
    r = em.install_packages(lines.append, _never)
    assert r.ok, r
    assert lines == ["Collecting onnxruntime", "Successfully installed onnxruntime tokenizers"]
    argv, kw = calls[0]
    assert argv == em.pip_install_argv()
    assert kw["env"]["PIP_NO_INPUT"] == "1" and kw["stdin"] == subprocess.DEVNULL


def test_install_packages_failure(monkeypatch):
    monkeypatch.setattr(em, "_popen", lambda argv, **kw: _FakeProc(
        [f"line {i}" for i in range(30)] + ["ERROR: no matching distribution"], rc=1))
    r = em.install_packages(lambda line: None, _never)
    assert (r.ok, r.error) == (False, "pip")
    assert r.message.splitlines()[-1] == "ERROR: no matching distribution"
    assert len(r.message.splitlines()) == 20


def test_install_packages_start_failure(monkeypatch):
    def popen(argv, **kw):
        raise FileNotFoundError("python")
    monkeypatch.setattr(em, "_popen", popen)
    r = em.install_packages(lambda line: None, _never)
    assert (r.ok, r.error) == (False, "pip")


def test_install_packages_cancel(monkeypatch):
    proc = _FakeProc(["Collecting onnxruntime"], hang=True)
    monkeypatch.setattr(em, "_popen", lambda argv, **kw: proc)
    r = em.install_packages(lambda line: None, lambda: True)
    assert (r.ok, r.error) == (False, "cancelled")
    assert proc.terminated


def test_readiness(monkeypatch):
    model = _make_model()
    monkeypatch.setitem(em.CATALOG, model.id, model)
    monkeypatch.setattr(em, "_find_spec", lambda n: None if n == "tokenizers" else object())
    r = em.readiness(model.id)
    assert r.deps_missing == ["tokenizers"] and r.model_state == "missing" and r.model is model
    _install_files(_root(), model)
    monkeypatch.setattr(em, "_find_spec", lambda n: object())
    r = em.readiness(model.id)
    assert r.deps_missing == [] and r.model_state == "installed"


# ---------------------------------------------------------------- 推論（偽の onnxruntime・tokenizers）


class _Enc:
    def __init__(self, ids, mask):
        self.ids = ids
        self.attention_mask = mask


class FakeTokenizer:
    """1 文字 = 1 トークン。id は文字コードから決める。pad の id は 3。"""

    last: FakeTokenizer | None = None

    def __init__(self):
        self.truncation = None
        self.pad = None
        self.seen: list[str] = []
        self.padding = None

    @classmethod
    def from_file(cls, path):
        t = cls()
        t.path = path
        FakeTokenizer.last = t
        return t

    def enable_truncation(self, max_length):
        self.truncation = max_length

    def enable_padding(self, pad_id=0, pad_token="<pad>"):
        self.pad = (pad_id, pad_token)

    def token_to_id(self, token):
        return {"<pad>": 3}.get(token)

    def encode_batch(self, texts):
        self.seen.extend(texts)
        cut = [t[:self.truncation] if self.truncation else t for t in texts]
        width = max(len(t) for t in cut)
        out = []
        for t in cut:
            ids = [10 + ord(c) % 500 for c in t]
            n = len(ids)
            out.append(_Enc(ids + [self.pad[0]] * (width - n), [1] * n + [0] * (width - n)))
        return out


def _hidden_for(ids: np.ndarray, mask: np.ndarray, dims: int) -> np.ndarray:
    """token i の隠れ状態 = one-hot(id % dims)。pad の位置は巨大な値（除外されなければ支配する）。"""
    b, length = ids.shape
    h = np.zeros((b, length, dims), dtype=np.float32)
    for i in range(b):
        for t in range(length):
            if mask[i, t]:
                h[i, t, ids[i, t] % dims] = 1.0 + (ids[i, t] % 7)
            else:
                h[i, t, :] = 1e6
    return h


class FakeSession:
    def __init__(self, ort, inputs, outputs, dims, transform=None):
        self.ort = ort
        self._inputs = inputs
        self._outputs = outputs
        self._dims = dims
        self.runs: list[dict] = []
        self.transform = transform

    def get_inputs(self):
        return [types.SimpleNamespace(name=n) for n in self._inputs]

    def get_outputs(self):
        return [types.SimpleNamespace(name=n) for n in self._outputs]

    def run(self, names, feed):
        self.runs.append({k: np.array(v) for k, v in feed.items()})
        h = _hidden_for(feed["input_ids"], feed["attention_mask"], self._dims)
        if self.transform is not None:
            h = self.transform(h)
        return [h]


class FakeOrt:
    def __init__(self, inputs=("input_ids", "attention_mask"), outputs=("last_hidden_state",),
                 providers=("CPUExecutionProvider",), dims=8, transform=None):
        self.inputs, self.outputs, self.providers = inputs, outputs, providers
        self.dims, self.transform = dims, transform
        self.sessions: list[FakeSession] = []
        self.created: list[dict] = []

    class SessionOptions:
        def __init__(self):
            self.intra_op_num_threads = 0
            self.inter_op_num_threads = 0

    def get_available_providers(self):
        return list(self.providers)

    def InferenceSession(self, path, sess_options=None, providers=None):
        self.created.append({"path": path, "opts": sess_options, "providers": providers})
        s = FakeSession(self, self.inputs, self.outputs, self.dims, self.transform)
        self.sessions.append(s)
        return s


@pytest.fixture
def fake_deps(monkeypatch):
    """依存がある・モデルが tmp の root に入っている状態（この PC には onnxruntime が無い）。"""
    model = _make_model()
    root = em.install_root()
    _install_files(root, model)
    ort = FakeOrt()
    monkeypatch.setattr(em, "_find_spec", lambda n: object())
    monkeypatch.setattr(em, "_import_deps", lambda: (ort, FakeTokenizer))
    return types.SimpleNamespace(model=model, root=root, ort=ort)


def _expected(text: str, dims=8) -> np.ndarray:
    ids = np.array([[10 + ord(c) % 500 for c in text]])
    h = _hidden_for(ids, np.ones_like(ids), dims)[0]
    v = h.mean(axis=0)
    return v / np.linalg.norm(v)


def test_embedder_not_ready_without_deps(monkeypatch):
    model = _make_model()
    _install_files(em.install_root(), model)
    monkeypatch.setattr(em, "_find_spec", lambda n: None)
    with pytest.raises(em.NotReady, match="missing: onnxruntime, tokenizers"):
        em.OnnxEmbedder(model, em.install_root())


def test_embedder_not_ready_import_failure(monkeypatch):
    model = _make_model()
    _install_files(em.install_root(), model)
    monkeypatch.setattr(em, "_find_spec", lambda n: object())

    def boom():
        raise ImportError("DLL load failed")
    monkeypatch.setattr(em, "_import_deps", boom)
    with pytest.raises(em.NotReady, match="import failed"):
        em.OnnxEmbedder(model, em.install_root())


def test_embedder_not_ready_without_model(monkeypatch):
    monkeypatch.setattr(em, "_find_spec", lambda n: object())
    with pytest.raises(em.NotReady, match="missing"):
        em.OnnxEmbedder(_make_model(), em.install_root())


def test_embedder_uses_given_root(fake_deps, tmp_path):
    # 渡された root で判定する（install_root() ではない）
    other = tmp_path / "elsewhere"
    with pytest.raises(em.NotReady):
        em.OnnxEmbedder(fake_deps.model, other)
    _install_files(other, fake_deps.model)
    e = em.OnnxEmbedder(fake_deps.model, other)
    assert str(other / "test-tiny") in fake_deps.ort.created[0]["path"]
    e.close()


def test_embed_documents_prefix_pooling_normalize(fake_deps):
    e = em.OnnxEmbedder(fake_deps.model, fake_deps.root)
    tok = FakeTokenizer.last
    assert tok.truncation == fake_deps.model.max_tokens
    assert tok.pad == (3, "<pad>")
    texts = ["a", "longer text here", "mid size"]
    v = e.embed_documents(texts)
    assert v.shape == (3, 8) and v.dtype == np.float32
    prefix = fake_deps.model.document_prefix
    assert sorted(tok.seen) == sorted(prefix + t for t in texts)
    for i, t in enumerate(texts):
        np.testing.assert_allclose(v[i], _expected(prefix + t), rtol=1e-5, atol=1e-6)
    np.testing.assert_allclose(np.linalg.norm(v, axis=1), 1.0, rtol=1e-5)
    q = e.embed_query("a")
    assert q.shape == (8,)
    np.testing.assert_allclose(q, _expected(fake_deps.model.query_prefix + "a"), rtol=1e-5, atol=1e-6)


def test_embed_batches_and_order(fake_deps):
    e = em.OnnxEmbedder(fake_deps.model, fake_deps.root)
    texts = [("x" * (40 - i)) + str(i) for i in range(40)]
    seen = []
    v = e.embed_documents(texts, progress=lambda d, n: seen.append((d, n)))
    sess = fake_deps.ort.sessions[0]
    assert len(sess.runs) == 3
    assert seen == [(16, 40), (32, 40), (40, 40)]
    prefix = fake_deps.model.document_prefix
    for i in (0, 17, 39):
        np.testing.assert_allclose(v[i], _expected(prefix + texts[i]), rtol=1e-5, atol=1e-6)
    # 長さ順に並べるので、最初のバッチは最も短い 16 件
    first_widths = sess.runs[0]["input_ids"].shape[1]
    last_widths = sess.runs[2]["input_ids"].shape[1]
    assert first_widths <= last_widths


def test_token_type_ids_get_zeros(monkeypatch, fake_deps):
    fake_deps.ort.inputs = ("input_ids", "attention_mask", "token_type_ids")
    e = em.OnnxEmbedder(fake_deps.model, fake_deps.root)
    e.embed_documents(["hello"])
    feed = fake_deps.ort.sessions[0].runs[0]
    assert set(feed) == {"input_ids", "attention_mask", "token_type_ids"}
    assert not feed["token_type_ids"].any()
    assert feed["token_type_ids"].shape == feed["input_ids"].shape


def test_unknown_input_is_rejected(fake_deps):
    fake_deps.ort.inputs = ("input_ids", "attention_mask", "position_ids")
    with pytest.raises(RuntimeError, match="unsupported model input: position_ids"):
        em.OnnxEmbedder(fake_deps.model, fake_deps.root)


def test_first_output_when_no_last_hidden_state(fake_deps):
    fake_deps.ort.outputs = ("token_embeddings", "sentence_embedding")
    e = em.OnnxEmbedder(fake_deps.model, fake_deps.root)
    assert e._output == "token_embeddings"
    assert e.embed_documents(["x"]).shape == (1, 8)


def test_bad_output_shape_and_nonfinite(fake_deps):
    fake_deps.ort.transform = lambda h: h[:, :, :4]
    e = em.OnnxEmbedder(fake_deps.model, fake_deps.root)
    with pytest.raises(RuntimeError, match="unexpected output shape"):
        e.embed_documents(["x"])
    fake_deps.ort.transform = lambda h: np.where(h == 0, np.nan, h)
    e = em.OnnxEmbedder(fake_deps.model, fake_deps.root)
    with pytest.raises(RuntimeError, match="non-finite"):
        e.embed_documents(["x"])


def test_providers_and_threads(fake_deps, monkeypatch):
    fake_deps.ort.providers = ("CPUExecutionProvider", "CUDAExecutionProvider")
    em.OnnxEmbedder(fake_deps.model, fake_deps.root)
    created = fake_deps.ort.created[-1]
    assert created["providers"] == ["CUDAExecutionProvider", "CPUExecutionProvider"]
    monkeypatch.setattr(os, "cpu_count", lambda: 6)
    fake_deps.ort.providers = ("CPUExecutionProvider",)
    em.OnnxEmbedder(fake_deps.model, fake_deps.root)
    created = fake_deps.ort.created[-1]
    assert created["providers"] == ["CPUExecutionProvider"]
    assert created["opts"].intra_op_num_threads == 5
    assert created["opts"].inter_op_num_threads == 1
    em.OnnxEmbedder(fake_deps.model, fake_deps.root, threads=2)
    assert fake_deps.ort.created[-1]["opts"].intra_op_num_threads == 2


def test_empty_input(fake_deps):
    e = em.OnnxEmbedder(fake_deps.model, fake_deps.root)
    v = e.embed_documents([])
    assert v.shape == (0, 8)
    assert fake_deps.ort.sessions[0].runs == []


def test_cancel_between_batches(fake_deps):
    e = em.OnnxEmbedder(fake_deps.model, fake_deps.root)
    seen = []
    with pytest.raises(em.Cancelled):
        e.embed_documents([f"t{i}" for i in range(40)],
                          progress=lambda d, n: seen.append(d),
                          cancel=lambda: len(seen) >= 1)
    assert seen == [16]
    assert len(fake_deps.ort.sessions[0].runs) == 1


def test_selfcheck(fake_deps, monkeypatch):
    e = em.OnnxEmbedder(fake_deps.model, fake_deps.root)

    def vecs(a_b, a_c, b_c):
        # 3 本の単位ベクトルを内積が指定どおりになるように作る（近似で十分）
        a = np.array([1.0, 0, 0])
        b = np.array([a_b, np.sqrt(1 - a_b ** 2), 0])
        cy = (b_c - a_b * a_c) / b[1]
        c = np.array([a_c, cy, np.sqrt(max(0.0, 1 - a_c ** 2 - cy ** 2))])
        return np.stack([a, b, c]).astype(np.float32)

    monkeypatch.setattr(e, "embed_documents", lambda texts: vecs(0.9, 0.2, 0.3))
    monkeypatch.setattr(e, "embed_query", lambda text: np.zeros(8, dtype=np.float32))
    c = e.selfcheck()
    assert c.ok and c.error is None and c.latency_ms >= 0
    monkeypatch.setattr(e, "embed_documents", lambda texts: vecs(0.2, 0.9, 0.3))
    c = e.selfcheck()
    assert not c.ok and c.error.startswith("order:")

    def boom(texts):
        raise RuntimeError("bad")
    monkeypatch.setattr(e, "embed_documents", boom)
    c = e.selfcheck()
    assert not c.ok and c.error == "RuntimeError: bad"


def test_close(fake_deps):
    e = em.OnnxEmbedder(fake_deps.model, fake_deps.root)
    e.close()
    with pytest.raises(em.NotReady, match="closed"):
        e.embed_documents(["x"])
    with pytest.raises(em.NotReady):
        e.embed_query("x")


# ---------------------------------------------------------------- 実モデル（手動）


def _real_root() -> Path:
    # conftest の install_root の差し替えを避けて実パスを直接読む（読むだけ）
    return Path.home() / ".myanalysis" / "embeddings"


@pytest.mark.skipif(os.environ.get("MYANALYSIS_TEST_EMBED_MODEL") != "1",
                    reason="実モデルの推論は MYANALYSIS_TEST_EMBED_MODEL=1 のときだけ")
@pytest.mark.parametrize("model_id", sorted(em.CATALOG))
def test_real_model_selfcheck(model_id):
    if em.missing_packages():
        pytest.skip("onnxruntime / tokenizers are not installed")
    model = em.CATALOG[model_id]
    if em._state_at(_real_root(), model).state != "installed":
        pytest.skip(f"{model_id} is not installed")
    e = em.OnnxEmbedder(model, _real_root())
    try:
        c = e.selfcheck()
        assert c.ok, c
        assert c.dims == model.dims
    finally:
        e.close()
