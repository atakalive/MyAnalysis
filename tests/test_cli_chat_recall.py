"""CLI `chat-recall` / `chat-index`（Issue #125）。

準備済みの状態は chat_index.load_prefs / readiness / make_embedder を差し替えて FakeEmbedder を返す。
埋め込み器は選択中のモデル（ruri-v3-30m）と同じ id・次元・recipe を名乗る（status・clear は
選択中のモデルの slug を見るため）。
"""
from __future__ import annotations

import hashlib
import json
import re

import numpy as np
import pytest

import config
import dataset_registry
import llm_bridge.__main__ as bridge_main
from common import embed_model, filelock
from common.chat_dataset import CHAT_DATASET_ENV
from llm_backend.base import Message
from llm_bridge import chat_index as ci
from llm_bridge import chat_store
from llm_bridge import paths as lb_paths

MODEL = embed_model.CATALOG["ruri-v3-30m"]


class FakeEmbedder:
    def __init__(self):
        self.model_id, self.dims, self.recipe = MODEL.id, MODEL.dims, MODEL.recipe
        self.calls: list[str] = []
        self.queries: list[str] = []
        self.fail_query = False

    def vec(self, text):
        seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "little")
        v = np.random.default_rng(seed).standard_normal(self.dims).astype(np.float32)
        return v / np.linalg.norm(v)

    def embed_documents(self, texts, *, progress=None, cancel=None):
        self.calls.extend(texts)
        if progress is not None:
            progress(len(texts), len(texts))
        return np.stack([self.vec(t) for t in texts]) if texts \
            else np.zeros((0, self.dims), np.float32)

    def embed_query(self, text):
        if self.fail_query:
            raise RuntimeError("onnx exploded")
        self.queries.append(text)
        return self.vec(text)

    def close(self):
        pass


@pytest.fixture
def ready(monkeypatch):
    emb = FakeEmbedder()
    monkeypatch.setattr(ci, "load_prefs", lambda: ci.RecallPrefs(True, MODEL.id, True))
    monkeypatch.setattr(ci, "readiness", lambda: embed_model.Readiness([], "installed", MODEL))
    monkeypatch.setattr(ci, "make_embedder", lambda: emb)
    return emb


def _register(tmp_path, name):
    d = tmp_path / "data" / name
    d.mkdir(parents=True, exist_ok=True)
    config.register_dataset(name, str(d))
    config.reload_datasets()
    return d


def _chat(ds_dir, sid, msgs, title="a chat"):
    s = chat_store.new_session("mock", "system", title=title)
    s.id = sid
    for role, content in msgs:
        s.messages.append(Message(role=role, content=content))
    chat_store.write_session_file(ds_dir / "_work", s)
    return s


SID_A = "aaaaaaaa" + "0" * 24
SID_B = "bbbbbbbb" + "0" * 24


@pytest.fixture
def corpus(tmp_path, ready):
    a = _register(tmp_path, "dsA")
    b = _register(tmp_path, "dsB")
    _chat(a, SID_A, [("user", "how to fix the needle peak"), ("assistant", "subtract the baseline")],
          title="peak fix")
    _chat(b, SID_B, [("user", "needle in dataset B")], title="other")
    ci.ChatIndex(ready).update()
    ready.calls.clear()
    return {"a": a, "b": b}


def _run(capsys, *argv):
    rc = bridge_main.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


def _json(capsys, *argv):
    rc, out, err = _run(capsys, *argv)
    assert rc == 0, err
    return json.loads(out), err


# ---------------------------------------------------------------- オフ・未導入


def test_off_exits_2_without_embedding(capsys, monkeypatch):
    monkeypatch.setattr(ci, "make_embedder", lambda: pytest.fail("must not build an embedder"))
    rc, out, err = _run(capsys, "chat-recall", "anything")
    assert rc == 2 and out == ""
    assert "error: chat recall is an experimental feature and is off" in err
    assert "Traceback" not in err


def test_missing_deps_exit_2(capsys, monkeypatch):
    lb_paths.update_ui_pref("chat_recall_enabled", True)
    monkeypatch.setattr(embed_model, "_find_spec", lambda n: None)
    rc, _out, err = _run(capsys, "chat-recall", "anything")
    assert rc == 2
    assert "missing packages: onnxruntime, tokenizers" in err
    assert "model ruri-v3-30m: missing" in err


def test_missing_model_exit_2(capsys, monkeypatch):
    lb_paths.update_ui_pref("chat_recall_enabled", True)
    monkeypatch.setattr(embed_model, "_find_spec", lambda n: object())
    rc, _out, err = _run(capsys, "chat-recall", "anything")
    assert rc == 2
    assert "model ruri-v3-30m: missing" in err and "missing packages" not in err


def test_embedder_not_ready_exit_2(capsys, ready, monkeypatch):
    def not_ready():
        raise ci.NotReady("import failed: DLL")
    monkeypatch.setattr(ci, "make_embedder", not_ready)
    rc, _out, err = _run(capsys, "chat-recall", "x")
    assert rc == 2 and "import failed: DLL" in err


# ---------------------------------------------------------------- 出力と終了コード


def test_json_shape_and_no_document_embedding(capsys, corpus, ready):
    data, _err = _json(capsys, "chat-recall", "needle", "peak", "--json")
    assert set(data) == {"query", "scope", "updated_index", "stale", "warnings", "hits"}
    assert data["query"] == "needle peak"
    assert data["scope"] == {"datasets": None, "include_archived": False}
    assert data["updated_index"] is False and data["stale"] == {} and data["warnings"] == []
    assert {h["dataset"] for h in data["hits"]} == {"dsA", "dsB"}
    h = data["hits"][0]
    assert set(h) == {"sid", "idx", "dataset", "title", "archived", "role", "score", "cosine",
                      "lexical", "snippet", "updated"}
    assert ready.calls == [] and ready.queries == ["needle peak"]


def test_expand_adds_full_text(capsys, corpus):
    data, _err = _json(capsys, "chat-recall", "baseline", "--dataset", "dsA", "--json", "--expand")
    hit = next(h for h in data["hits"] if h["idx"] == 2)
    assert hit["text"] == "subtract the baseline"
    rc, out, _err = _run(capsys, "chat-recall", "baseline", "--dataset", "dsA", "--expand")
    assert rc == 0 and "    subtract the baseline" in out


def test_human_output_format(capsys, corpus):
    rc, out, err = _run(capsys, "chat-recall", "needle", "--dataset", "dsA", "-k", "1")
    assert rc == 0, err
    lines = out.splitlines()
    assert re.fullmatch(r"1\. sid=aaaaaaaa idx=\d+ \| ds=dsA \| peak fix \| (user|assistant) \| "
                        r"\d{4}-\d\d-\d\d \d\d:\d\d \| score=\d\.\d{4}", lines[0]), lines[0]
    assert lines[1].startswith("    ")
    assert len([ln for ln in lines if re.match(r"\d+\. ", ln)]) == 1


def test_zero_hits_exit_0(capsys, tmp_path, ready):
    _register(tmp_path, "empty")
    rc, out, err = _run(capsys, "chat-recall", "nothing")
    assert rc == 0 and out == "" and "no matches" in err


def test_inference_failure_exit_1(capsys, corpus, ready):
    ready.fail_query = True
    rc, _out, err = _run(capsys, "chat-recall", "needle")
    assert rc == 1
    assert "error: chat recall failed: RuntimeError: onnx exploded" in err
    assert "Traceback" not in err


def test_registry_error_exit_1(capsys, ready, monkeypatch):
    def broken(*a, **k):
        raise dataset_registry.RegistryError("registry is corrupt")
    monkeypatch.setattr(config, "reload_datasets", broken)
    rc, _out, err = _run(capsys, "chat-recall", "x")
    assert rc == 1 and "registry is corrupt" in err


# ---------------------------------------------------------------- 範囲


def _scope(capsys, *extra):
    data, _err = _json(capsys, "chat-recall", "needle", "--json", *extra)
    return data["scope"]["datasets"], {h["dataset"] for h in data["hits"]}


def test_scope_precedence(capsys, corpus, monkeypatch):
    assert _scope(capsys) == (None, {"dsA", "dsB"})
    monkeypatch.setenv(CHAT_DATASET_ENV, "dsB")
    assert _scope(capsys) == (["dsB"], {"dsB"})
    assert _scope(capsys, "--dataset", "dsA") == (["dsA"], {"dsA"})
    assert _scope(capsys, "--all") == (None, {"dsA", "dsB"})


def test_scope_errors(capsys, corpus, monkeypatch):
    with pytest.raises(SystemExit) as ei:
        bridge_main.main(["chat-recall", "x", "--dataset", "dsA", "--all"])
    assert ei.value.code == 2
    rc, _out, err = _run(capsys, "chat-recall", "x", "--dataset", "nope")
    assert rc == 1 and "unknown dataset 'nope'" in err
    monkeypatch.setenv(CHAT_DATASET_ENV, "gone")
    rc, _out, err = _run(capsys, "chat-recall", "x")
    assert rc == 1 and "unknown dataset 'gone'" in err


@pytest.mark.parametrize("argv", [
    ["--budget", "5"],
    ["--update", "--budget", "nan"],
    ["--update", "--budget", "inf"],
    ["--update", "--budget", "0"],
    ["-k", "0"],
    ["-k", "201"],
])
def test_argument_errors_exit_2(capsys, argv):
    with pytest.raises(SystemExit) as ei:
        bridge_main.main(["chat-recall", "x", *argv])
    assert ei.value.code == 2


# ---------------------------------------------------------------- --update と遅れ


def test_update_brings_new_chat_in(capsys, corpus, ready):
    _chat(corpus["a"], "cccccccc" + "0" * 24, [("user", "needle fresh")], title="fresh")
    rc, _out, err = _run(capsys, "chat-recall", "needle", "--dataset", "dsA")
    assert rc == 0 and "index is behind the saved chats: dsA 1 file" in err
    data, _err = _json(capsys, "chat-recall", "needle", "--dataset", "dsA", "--json", "--update")
    assert data["updated_index"] is True and data["stale"] == {}
    assert "cccccccc" in {h["sid"] for h in data["hits"]}
    assert ready.calls == ["needle fresh"]


def test_update_while_locked_warns(capsys, corpus, ready):
    _chat(corpus["a"], "cccccccc" + "0" * 24, [("user", "needle fresh")])
    with filelock.exclusive_lock(lb_paths.chat_index_dir() / f"{MODEL.id}.lock"):
        rc, _out, err = _run(capsys, "chat-recall", "needle", "--update")
        data, _e = _json(capsys, "chat-recall", "needle", "--update", "--json")
    assert rc == 0
    assert "warning: another process is updating the chat index" in err
    assert data["updated_index"] is False
    assert [w["code"] for w in data["warnings"]] == ["locked"]
    assert data["stale"] == {"dsA": 1}
    assert ready.calls == []


# ---------------------------------------------------------------- chat-index


def test_chat_index_status_works_when_off(capsys, tmp_path):
    _register(tmp_path, "dsA")
    rc, out, _err = _run(capsys, "chat-index", "status")
    assert rc == 0
    assert out.splitlines()[0].startswith("model=ruri-v3-30m dims=256 enabled=false ready=false")
    assert re.search(r"^dsA  sessions=0 chunks=0 size=0 B updated=- stale=0$", out, re.MULTILINE)
    data, _err = _json(capsys, "chat-index", "status", "--json")
    assert data["enabled"] is False and data["datasets"][0]["name"] == "dsA"


def test_chat_index_update_rebuild_clear(capsys, tmp_path, ready):
    a = _register(tmp_path, "dsA")
    _chat(a, SID_A, [("user", "one"), ("assistant", "two")])
    rc, out, _err = _run(capsys, "chat-index", "update")
    assert rc == 0
    assert "dsA: updated sessions=1 chunks=2 embedded=2 reused=0" in out
    assert re.search(r"^done in \d+\.\d s$", out, re.MULTILINE)
    rc, out, _err = _run(capsys, "chat-index", "update")
    assert rc == 0 and "dsA: unchanged" in out
    data, _err = _json(capsys, "chat-index", "status", "--json")
    assert data["datasets"][0]["chunks"] == 2 and data["datasets"][0]["sessions"] == 1
    ready.calls.clear()
    data, _err = _json(capsys, "chat-index", "rebuild", "--json")
    assert data["ran"] is True and sorted(ready.calls) == ["one", "two"]
    rc, out, _err = _run(capsys, "chat-index", "clear", "--dataset", "dsA")
    assert rc == 0 and out.strip() == "cleared 1 shard(s)"
    data, _err = _json(capsys, "chat-index", "clear", "--json")
    assert data == {"cleared": 0}


def test_chat_index_update_when_off_exits_2(capsys):
    rc, _out, err = _run(capsys, "chat-index", "update")
    assert rc == 2 and "is off" in err


def test_chat_index_unknown_dataset(capsys, ready):
    rc, _out, err = _run(capsys, "chat-index", "update", "--dataset", "nope")
    assert rc == 1 and "unknown dataset 'nope'" in err


def test_chat_index_lock_timeout_and_other_oserror(capsys, tmp_path, ready, monkeypatch):
    _register(tmp_path, "dsA")

    def timeout(path):
        raise filelock.LockTimeout(36, "lock timeout")

    monkeypatch.setattr(filelock, "exclusive_lock", timeout)
    rc, _out, err = _run(capsys, "chat-index", "update")
    assert rc == 1 and err.strip() == "error: another process holds the chat index lock"

    def denied(path):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(filelock, "exclusive_lock", denied)
    rc, _out, err = _run(capsys, "chat-index", "update")
    assert rc == 1 and err.startswith("error: PermissionError")
    assert "another process" not in err
