"""llm_backend.model_catalog — 状況ウィンドウの「モデル取得」（Qt 非依存）。

**実プロセスは一切起動しない**: ``Popen`` / ``preflight._run`` / ``preflight._which`` を
差し替える。models.toml に書き得るテストは ``engines.models_toml_path`` を tmp に向ける。
"""
from __future__ import annotations

import json
import threading
import tomllib

import pytest

from llm_backend import engines, model_catalog, preflight
from llm_backend.claude_code import ClaudeCodeBackend
from llm_backend.model_catalog import AppendOutcome, ModelList, append_fetched, append_new
from llm_backend.settings_store import set_toml_keys


@pytest.fixture(autouse=True)
def _no_real_processes(monkeypatch):
    monkeypatch.setattr(preflight, "_which", lambda name: None)
    monkeypatch.setattr(preflight, "_run", lambda cmd, timeout: None)


def _settings(monkeypatch, **kw):
    monkeypatch.setattr(model_catalog, "_engine_settings", lambda eid: dict(kw))


def _catalogs():
    from common.paths import i18n_dir
    out = {}
    for lang in ("en", "ja"):
        with open(i18n_dir() / f"{lang}.toml", "rb") as f:
            out[lang] = tomllib.load(f)
    return out


def _assert_notes_resolve(notes):
    for lang, cat in _catalogs().items():
        for key, params in notes:
            assert key in cat, f"{key} missing from {lang}.toml"
            cat[key].format(**params)


# ---- claude ----


def _claude_body(monkeypatch, body, source="/opt/claude"):
    monkeypatch.setattr(ClaudeCodeBackend, "_discover_binary", lambda self: source)
    monkeypatch.setattr(ClaudeCodeBackend, "list_models", lambda self, timeout=20.0: body)


def test_claude_excludes_default_and_collects_aliases(monkeypatch):
    _settings(monkeypatch, bin="")
    _claude_body(monkeypatch, {"models": [
        {"value": "default", "resolvedModel": "claude-opus-5-5"},
        {"value": "opus", "resolvedModel": "claude-opus-5-5"},
        {"value": "claude-fable-5-1", "resolvedModel": "claude-fable-5-1"},
        {"value": "haiku", "resolvedModel": ""},
    ]})
    ml = model_catalog.fetch_models("claude-vscode")
    assert ml.state == "ok"
    assert ml.models == ("opus", "claude-fable-5-1", "haiku")
    assert ml.aliases == (("opus", "claude-opus-5-5"),)
    assert ml.source == "/opt/claude"


@pytest.mark.parametrize("body", [{}, {"models": "nope"}, {"models": None}])
def test_claude_without_models_list_is_unknown(monkeypatch, body):
    _settings(monkeypatch, bin="claude")
    _claude_body(monkeypatch, body)
    ml = model_catalog.fetch_models("claude-cli")
    assert ml.state == "unknown"
    assert ml.notes == (("backend.models.claude_no_models", {}),)
    _assert_notes_resolve(ml.notes)


def test_claude_skips_bad_values_and_dedupes(monkeypatch):
    _settings(monkeypatch, bin="")
    _claude_body(monkeypatch, {"models": [
        "str-item", {"value": 3}, {"value": "  "}, {"value": " sonnet "},
        {"value": "sonnet"}, {"novalue": 1},
    ]})
    ml = model_catalog.fetch_models("claude-vscode")
    assert ml.models == ("sonnet",)


# ---- ClaudeCodeBackend.list_models 本体 ----


class _FakeStdin:
    def __init__(self):
        self.written: list[bytes] = []
        self.closed = False

    def write(self, b):
        self.written.append(b)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _FakeProc:
    def __init__(self, lines, block: threading.Event | None = None):
        self.stdin = _FakeStdin()
        self._lines = lines
        self._block = block
        self.stderr = iter(())
        self.pid = 4242
        self.returncode = None

    @property
    def stdout(self):
        def gen():
            for ln in self._lines:
                yield ln
            if self._block is not None:
                self._block.wait(5)
            self.returncode = 0 if self.returncode is None else self.returncode
        return gen()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


@pytest.fixture()
def claude_env(monkeypatch, tmp_path):
    import llm_backend.claude_code as cc
    state = {"popen": [], "killed": [], "procs": []}
    monkeypatch.setattr(cc.uuid, "uuid4", lambda: type("U", (), {"hex": "rid1"})())
    monkeypatch.setattr(ClaudeCodeBackend, "_discover_binary", lambda self: "/opt/claude")
    monkeypatch.setattr(ClaudeCodeBackend, "_agent_home", staticmethod(lambda: tmp_path))

    def _kill(self, proc):
        state["killed"].append(proc)
        if proc._block is not None:
            proc.returncode = -15
            proc._block.set()
    monkeypatch.setattr(ClaudeCodeBackend, "_kill_tree", _kill)

    def install(lines, block=None):
        def _popen(cmd, **kw):
            p = _FakeProc(lines, block)
            state["popen"].append((cmd, kw))
            state["procs"].append(p)
            return p
        monkeypatch.setattr(cc.subprocess, "Popen", _popen)
    state["install"] = install
    return state


def _resp(rid, subtype="success", **extra):
    return (json.dumps({"type": "control_response",
                        "response": {"subtype": subtype, "request_id": rid, **extra}})
            + "\n").encode()


def test_list_models_skips_unrelated_lines(claude_env):
    claude_env["install"]([
        b"not json\n",
        b"\n",
        (json.dumps({"type": "system", "subtype": "init"}) + "\n").encode(),
        _resp("other", response={"models": ["wrong"]}),
        (json.dumps({"type": "control_response", "response": {"subtype": "success"}})
         + "\n").encode(),
        _resp("rid1", response={"models": [{"value": "opus"}]}),
    ])
    body = ClaudeCodeBackend({}).list_models()
    assert body == {"models": [{"value": "opus"}]}
    proc = claude_env["procs"][0]
    assert proc.stdin.closed


def test_list_models_error_subtype_raises(claude_env):
    claude_env["install"]([_resp("rid1", subtype="error", error="nope bad")])
    with pytest.raises(RuntimeError, match="nope bad"):
        ClaudeCodeBackend({}).list_models()


def test_list_models_eof_before_answer_raises(claude_env):
    claude_env["install"]([b"garbage\n"])
    with pytest.raises(RuntimeError, match="before answering initialize"):
        ClaudeCodeBackend({}).list_models()


def test_list_models_timeout_kills_and_raises(claude_env):
    claude_env["install"]([], block=threading.Event())
    with pytest.raises(RuntimeError, match="timed out"):
        ClaudeCodeBackend({}).list_models(timeout=0.2)
    assert claude_env["killed"]


def test_list_models_argv_and_spawn_kwargs(claude_env, tmp_path, monkeypatch):
    import llm_backend.claude_code as cc
    monkeypatch.setattr(cc.sys, "platform", "linux")
    claude_env["install"]([_resp("rid1", response={})])
    ClaudeCodeBackend({}).list_models()
    cmd, kw = claude_env["popen"][0]
    assert cmd == ["/opt/claude", "--output-format", "stream-json",
                   "--input-format", "stream-json", "--verbose"]
    assert kw["start_new_session"] is True
    assert kw["cwd"] == str(tmp_path)


def test_list_models_sends_only_initialize(claude_env):
    """トークンを使わない: user メッセージを送らず、initialize をちょうど 1 行だけ。"""
    claude_env["install"]([_resp("rid1", response={"models": []})])
    ClaudeCodeBackend({}).list_models()
    sent = [json.loads(b) for b in claude_env["procs"][0].stdin.written]
    assert not any(m.get("type") == "user" for m in sent)
    inits = [m for m in sent if m.get("type") == "control_request"
             and m.get("request", {}).get("subtype") == "initialize"]
    assert len(inits) == 1 and len(sent) == 1
    assert inits[0]["request_id"] == "rid1"
    for cmd, _ in claude_env["popen"]:
        for bad in ("auth", "exec", "--print", "-p"):
            assert bad not in cmd


# ---- pi ----

_LISTING = (
    "provider        model            context\n"
    "lonely\n"
    "anthropic       claude-opus-5    1M\n"
    "openai-codex    gpt-6-sol        272K\n"
    "github-copilot  gpt-6-sol        1M\n"
    "github-copilot  gpt-5.4          1M\n"
    "llama.cpp       qwen3            32K\n"
)


def _pi(monkeypatch, out=(0, _LISTING)):
    calls = {"which": [], "run": []}

    def _which(name):
        calls["which"].append(name)
        return f"/usr/bin/{name}"

    def _run(cmd, timeout):
        calls["run"].append(list(cmd))
        return out
    monkeypatch.setattr(preflight, "_which", _which)
    monkeypatch.setattr(preflight, "_run", _run)
    return calls


def test_pi_filters_providers_and_dedupes_in_order(monkeypatch):
    _settings(monkeypatch)
    _pi(monkeypatch)
    ml = model_catalog.fetch_models("pi")
    assert ml.state == "ok"
    assert ml.models == ("gpt-6-sol", "gpt-5.4", "qwen3")
    assert ml.detail == "openai-codex, github-copilot, llama.cpp"
    assert ml.source == "/usr/bin/pi --list-models"


def test_pi_configured_provider_limits_rows(monkeypatch):
    _settings(monkeypatch, provider="github-copilot")
    _pi(monkeypatch)
    ml = model_catalog.fetch_models("pi")
    assert ml.models == ("gpt-6-sol", "gpt-5.4")
    assert ml.detail == "github-copilot"


def test_pi_provider_outside_allowed_does_not_spawn(monkeypatch):
    _settings(monkeypatch, provider="anthropic")
    calls = _pi(monkeypatch)
    ml = model_catalog.fetch_models("pi")
    assert ml.state == "unknown"
    assert ml.notes[0][0] == "backend.models.pi_provider_unsupported"
    assert calls["run"] == []
    _assert_notes_resolve(ml.notes)


@pytest.mark.parametrize("out", [None, (1, "\nboom: offline\n")])
def test_pi_run_failure_is_unknown(monkeypatch, out):
    _settings(monkeypatch)
    _pi(monkeypatch, out=out)
    ml = model_catalog.fetch_models("pi")
    assert ml.state == "unknown"
    if out is not None:
        assert ml.detail == "pi --list-models exited with code 1: boom: offline"


def test_pi_not_found_is_missing(monkeypatch):
    _settings(monkeypatch)
    assert model_catalog.fetch_models("pi").state == "missing"


def test_pi_configured_bin_is_resolved(monkeypatch):
    _settings(monkeypatch, bin="mypi")
    calls = _pi(monkeypatch)
    model_catalog.fetch_models("pi")
    assert calls["which"] == ["mypi"]
    assert calls["run"] == [["/usr/bin/mypi", "--list-models"]]
    for cmd in calls["run"]:
        for bad in ("auth", "exec", "--print", "-p"):
            assert bad not in cmd


# ---- codex ----


def _cache(tmp_path, data):
    p = tmp_path / "models_cache.json"
    p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return p


def test_codex_visibility_priority_and_slugs(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _cache(tmp_path, {"fetched_at": "2026-10-01T00:00:00Z", "models": [
        {"slug": "c", "visibility": "list", "priority": 3},
        {"slug": "hidden", "visibility": "hide", "priority": 0},
        {"slug": "a", "visibility": "list", "priority": 1},
        {"slug": "nokey", "priority": 2},
        {"slug": "b", "priority": 1},
        {"slug": "nopri"},
        {"slug": "strpri", "priority": "1"},
        {"slug": "boolpri", "priority": True},
        {"slug": "", "priority": 0},
        {"slug": 5, "priority": 0},
        "junk",
        {"slug": "a", "priority": 9},
    ]})
    ml = model_catalog.fetch_models("codex")
    assert ml.state == "ok"
    assert ml.models == ("a", "b", "nokey", "c", "nopri", "strpri", "boolpri")
    assert ml.fetched_at == "2026-10-01T00:00:00Z"
    assert ml.source == str(tmp_path / "models_cache.json")


def test_codex_priority_huge_int_and_non_finite_floats(monkeypatch, tmp_path):
    """整数は任意精度で有限。float に変換すると 10**400 で OverflowError になり、
    1 件のためにカタログ全体が unknown に落ちていた。"""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _cache(tmp_path, {"models": [
        {"slug": "nan", "priority": float("nan")},
        {"slug": "huge", "priority": 10**400},
        {"slug": "inf", "priority": float("inf")},
        {"slug": "small", "priority": 1},
        {"slug": "float", "priority": 2.5},
    ]})
    ml = model_catalog.fetch_models("codex")
    assert ml.state == "ok", ml.detail
    assert ml.models == ("small", "float", "huge", "nan", "inf")


@pytest.mark.parametrize("data", ["{not json", {"models": "x"}, [1, 2]])
def test_codex_broken_cache_is_unknown(monkeypatch, tmp_path, data):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _cache(tmp_path, data)
    assert model_catalog.fetch_models("codex").state == "unknown"


def test_codex_missing_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    ml = model_catalog.fetch_models("codex")
    assert ml.state == "missing"
    assert ml.notes[0][0] == "backend.models.codex_cache_missing"
    _assert_notes_resolve(ml.notes)


def test_codex_home_unset_reads_home_dot_codex(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(model_catalog.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".codex").mkdir()
    _cache(tmp_path / ".codex", {"models": [{"slug": "x"}]})
    assert model_catalog.fetch_models("codex").models == ("x",)


def test_codex_never_spawns(monkeypatch, tmp_path):
    import llm_backend.claude_code as cc
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _cache(tmp_path, {"models": [{"slug": "x"}]})
    spawned = []
    monkeypatch.setattr(cc.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(preflight, "_run", lambda *a, **k: spawned.append(a))
    assert model_catalog.fetch_models("codex").state == "ok"
    assert spawned == []


# ---- append_new ----


def test_append_new_order_and_cleaning():
    merged, added, absent = append_new(
        ["seed1", "seed2"], [" new1 ", "seed1", "", 3, "new1", "new2"]
    )
    assert added == ["new1", "new2"]
    assert merged == ["seed1", "seed2", "new1", "new2"]
    assert absent == ["seed2"]


def test_append_new_nothing_new():
    merged, added, absent = append_new(["a", "b"], ["b", "a"])
    assert added == [] and merged == ["a", "b"] and absent == []


def test_append_new_also_present_suppresses_absent():
    _, _, absent = append_new(["x", "claude-sonnet-5", "y"], ["sonnet"],
                              also_present=["claude-sonnet-5"])
    assert absent == ["x", "y"]


# ---- append_fetched ----


@pytest.fixture()
def models_file(monkeypatch, tmp_path):
    p = tmp_path / "models.toml"
    monkeypatch.setattr(engines, "models_toml_path", lambda: p)

    class _Fake:
        def __init__(self):
            self.data = {}
            self.cleared = 0

        def __call__(self):
            return self.data

        def cache_clear(self):
            self.cleared += 1
    fake = _Fake()
    monkeypatch.setattr(engines, "model_config", fake)
    return p, fake


_CODEX = engines.engine_by_id("codex")


def _choices(p, section="codex"):
    return tomllib.loads(p.read_text(encoding="utf-8"))[section]["model_choices"]


def test_append_fetched_uses_the_list_on_disk(models_file):
    p, fake = models_file
    set_toml_keys(p, {"codex": {"model_choices": ["A", "X"]}})
    fake.data = {"codex": {"model_choices": ["A", "X"]}}
    set_toml_keys(p, {"codex": {"model_choices": ["A", "Y"]}})   # 別の書き手
    out = append_fetched(_CODEX, ModelList("codex", "ok", models=("A", "B")))
    assert _choices(p) == ["A", "Y", "B"]
    assert out == AppendOutcome(added=("B",), absent=("Y",), written=True)


def test_append_fetched_readds_listed_ids(models_file):
    p, _ = models_file
    set_toml_keys(p, {"codex": {"model_choices": ["A"]}})
    append_fetched(_CODEX, ModelList("codex", "ok", models=("A", "X")))
    assert _choices(p) == ["A", "X"]


def test_append_fetched_nothing_new_writes_nothing(models_file):
    p, _ = models_file
    out = append_fetched(_CODEX, ModelList("codex", "ok",
                                           models=engines.OPENAI_CODEX_MODELS[:2]))
    assert out.written is False and out.added == ()
    assert not p.exists()
    set_toml_keys(p, {"codex": {"model_choices": ["A"]}})
    before = p.read_bytes()
    assert append_fetched(_CODEX, ModelList("codex", "ok", models=("A",))).written is False
    assert p.read_bytes() == before


def test_append_fetched_unset_appends_to_the_seed(models_file):
    p, _ = models_file
    append_fetched(_CODEX, ModelList("codex", "ok", models=("brand-new",)))
    assert _choices(p) == [*engines.OPENAI_CODEX_MODELS, "brand-new"]


def test_append_fetched_not_ok_does_nothing(models_file):
    p, _ = models_file
    assert append_fetched(_CODEX, ModelList("codex", "missing")) == AppendOutcome()
    assert not p.exists()


def test_append_fetched_alias_target_is_not_absent(models_file):
    p, _ = models_file
    eng = engines.engine_by_id("claude-vscode")
    set_toml_keys(p, {"claude_code": {"model_choices": ["claude-sonnet-5"]}})
    out = append_fetched(eng, ModelList("claude-vscode", "ok", models=("sonnet",),
                                        aliases=(("sonnet", "claude-sonnet-5"),)))
    assert out.absent == ()
    assert _choices(p, "claude_code") == ["claude-sonnet-5", "sonnet"]


# ---- 入口 ----


@pytest.mark.parametrize("eid", ["openai-http", "mock", "nope"])
def test_unsupported_engines_are_unknown(eid):
    assert model_catalog.fetch_models(eid).state == "unknown"


def test_supported_ids_exist_in_catalog():
    ids = {e.id for e in engines.ENGINES}
    assert set(model_catalog.SUPPORTED) <= ids


def test_fetch_models_never_raises(monkeypatch, tmp_path):
    def _boom(*a, **k):
        raise ValueError("boom")
    monkeypatch.setattr(model_catalog, "_engine_settings", _boom)
    for eid in ("claude-vscode", "claude-cli", "pi"):
        ml = model_catalog.fetch_models(eid)
        assert ml.state == "unknown" and "boom" in ml.detail
    monkeypatch.setattr(model_catalog, "_fetch_codex", _boom)
    assert model_catalog.fetch_models("codex").state == "unknown"


# ---- トークンを使わない ----


def test_fetch_never_builds_pings_or_streams(monkeypatch, tmp_path):
    import llm_backend
    import llm_backend.ping as ping
    from llm_backend.codex import CodexBackend
    from llm_backend.pi import PiCodingAgentBackend

    called = []

    def _rec(name):
        return lambda *a, **k: called.append(name)
    monkeypatch.setattr(ping, "ping_backend", _rec("ping"))
    monkeypatch.setattr(llm_backend, "build_backend", _rec("build"))
    monkeypatch.setattr(ClaudeCodeBackend, "stream", _rec("claude.stream"))
    monkeypatch.setattr(PiCodingAgentBackend, "stream", _rec("pi.stream"))
    monkeypatch.setattr(CodexBackend, "stream", _rec("codex.stream"))

    _settings(monkeypatch)
    _claude_body(monkeypatch, {"models": [{"value": "opus"}]})
    _pi(monkeypatch)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _cache(tmp_path, {"models": [{"slug": "x"}]})
    for eid in model_catalog.SUPPORTED:
        assert model_catalog.fetch_models(eid).state == "ok", eid
    assert called == []

