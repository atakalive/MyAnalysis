"""Offline / hermetic tests for config_share (R2 config push/pull).

No network: config_share._client is monkeypatched to a FakeS3 (never imports
boto3). The real datasets.local.json and real ~/.myanalysis are never touched:
sidecar state, repo_root, and config_path are all redirected under tmp_path.
"""

from __future__ import annotations

import json
import socket

import pytest

import config
import config_share


# --------------------------------------------------------------------------- #
# Fake S3 (single in-memory object + deterministic ETag)
# --------------------------------------------------------------------------- #
class FakeClientError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _Body:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data


class FakeS3:
    def __init__(self):
        self.body: bytes | None = None
        self.etag: str | None = None
        self._counter = 0

    # test helper: seed a remote bundle
    def seed(self, bundle) -> None:
        self.body = (bundle if isinstance(bundle, bytes)
                     else json.dumps(bundle, ensure_ascii=False).encode("utf-8"))
        self._counter += 1
        self.etag = f'"etag{self._counter}"'

    def stored(self):
        return json.loads(self.body) if self.body is not None else None

    def get_object(self, Bucket, Key):
        if self.body is None:
            raise FakeClientError("NoSuchKey")
        return {"Body": _Body(self.body), "ETag": self.etag}

    def put_object(self, Bucket, Key, Body, ContentType=None,
                   IfMatch=None, IfNoneMatch=None):
        if IfNoneMatch == "*" and self.body is not None:
            raise FakeClientError("PreconditionFailed")
        if IfMatch is not None and IfMatch != self.etag:
            raise FakeClientError("PreconditionFailed")
        self.body = Body
        self._counter += 1
        self.etag = f'"etag{self._counter}"'
        return {"ETag": self.etag}


def _write_config(path, datasets: dict) -> None:
    """登録簿 JSON を書く（旧 config.py テンプレートの置き換え）。"""
    path.write_text(
        json.dumps(datasets, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )


def _datasets_from(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Hermetic environment: fake client, isolated state/repo_root, fixed host."""
    fake = FakeS3()
    monkeypatch.setattr(config_share, "_client", lambda creds: fake)
    monkeypatch.setattr(config_share, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(config_share, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "SELF")

    for k in ("R2_ENDPOINT_URL", "R2_BUCKET", "R2_ACCESS_KEY_ID",
              "R2_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "x")
    monkeypatch.delenv("R2_PREFIX", raising=False)
    monkeypatch.delenv("R2_AUTOSYNC", raising=False)
    # load_env() must not read the real repo .env
    monkeypatch.setattr(config_share, "load_env", lambda *a, **k: None)

    cfg = tmp_path / "datasets.local.json"
    _write_config(cfg, {"ds_local": {"SELF": "/local/p"}})

    class Env:
        pass

    e = Env()
    e.fake = fake
    e.cfg = cfg
    e.tmp = tmp_path
    return e


def _sync(env, **kw):
    kw.setdefault("config_path", env.cfg)
    return config_share.sync(**kw)


# --------------------------------------------------------------------------- #
# 1. write_registry
# --------------------------------------------------------------------------- #
def test_write_registry_roundtrip(tmp_path):
    cfg = tmp_path / "datasets.local.json"
    _write_config(cfg, {"a": {"H1": r"G:\a\b"}})
    config.write_registry(
        {"a": {"H1": r"G:\a\b"}, "b": {"H2": "/x/y"}}, config_path=cfg
    )
    data = _datasets_from(cfg)
    assert data == {"a": {"H1": r"G:\a\b"}, "b": {"H2": "/x/y"}}
    # JSON escapes backslashes; the value roundtrips unchanged.
    assert r'"G:\\a\\b"' in cfg.read_text(encoding="utf-8")


def test_write_registry_output_format(tmp_path):
    cfg = tmp_path / "datasets.local.json"
    _write_config(cfg, {"a": {"H1": "/p"}})
    config.write_registry({"a": {"H1": "/p"}, "b": {"H2": "/q"}}, config_path=cfg)
    raw = cfg.read_bytes()
    assert b"\r\n" not in raw                       # LF only
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    assert not raw.startswith(b"\xef\xbb\xbf")      # no BOM


# --------------------------------------------------------------------------- #
# 2. make_bundle / _collect_portable_files
# --------------------------------------------------------------------------- #
def test_make_bundle_basics(env):
    (env.tmp / "models.toml").write_text("m=1", encoding="utf-8")
    (env.tmp / ".env").write_text("SECRET=1", encoding="utf-8")
    b = config_share.make_bundle(config_path=env.cfg)
    assert b["datasets"] == {"ds_local": {"SELF": "/local/p"}}
    assert "models.toml" in b["files"]
    assert ".env" not in b["files"]                 # excluded by default
    assert "models.toml" in b["file_meta"]
    # new local entry has no entry_meta yet
    assert "ds_local/SELF" not in b["entry_meta"]


def test_make_bundle_include_env(env):
    (env.tmp / ".env").write_text("SECRET=1", encoding="utf-8")
    b = config_share.make_bundle(config_path=env.cfg, include_env=True)
    assert ".env" in b["files"]


def test_collect_unreadable_excluded(env):
    (env.tmp / "models.toml").write_bytes(b"\xff\xfe\x00bad")
    warnings: list = []
    files, file_meta, unreadable = config_share._collect_portable_files(
        include_env=False, warnings=warnings
    )
    assert "models.toml" not in files
    assert "models.toml" in unreadable
    assert warnings


def test_make_bundle_none_warnings(env):
    (env.tmp / "models.toml").write_bytes(b"\xff\xfe\x00bad")
    # must not raise when warnings omitted
    b = config_share.make_bundle(config_path=env.cfg)
    assert "models.toml" not in b["files"]


# --------------------------------------------------------------------------- #
# 3. Fallback (no-op when not configured / boto3 missing)
# --------------------------------------------------------------------------- #
def test_not_configured(env, monkeypatch):
    monkeypatch.delenv("R2_BUCKET", raising=False)
    assert config_share.is_configured() is False
    assert config_share.try_sync(config_path=env.cfg) is None


def test_autosync_disabled(env, monkeypatch):
    monkeypatch.setenv("R2_AUTOSYNC", "0")
    assert config_share.try_sync(config_path=env.cfg) is None


def test_try_sync_swallows_import_error(env, monkeypatch):
    def boom(creds):
        raise ImportError("no boto3")
    monkeypatch.setattr(config_share, "_client", boom)
    assert config_share.try_sync(config_path=env.cfg) is None


# --------------------------------------------------------------------------- #
# 4. Convergence / idempotence
# --------------------------------------------------------------------------- #
def test_union_and_idempotent(env):
    env.fake.seed({
        "schema_version": 1, "updated_by": "B", "updated_at": config_share._now_iso(),
        "datasets": {"ds_remote": {"B": "/r/p"}},
    })
    r1 = _sync(env)
    assert r1["pushed"] is True
    data = _datasets_from(env.cfg)
    assert "ds_local" in data and "ds_remote" in data
    stored = env.fake.stored()
    assert "ds_local" in stored["datasets"] and "ds_remote" in stored["datasets"]

    r2 = _sync(env)
    assert r2["pushed"] is False
    assert _datasets_from(env.cfg) == data


# --------------------------------------------------------------------------- #
# 5. Self-host priority
# --------------------------------------------------------------------------- #
def test_self_host_priority(env):
    env.fake.seed({
        "schema_version": 1, "datasets": {"ds_local": {"SELF": "/remote/wins?"}},
        "entry_meta": {"ds_local/SELF": "2999-01-01T00:00:00+00:00"},
    })
    _sync(env)
    data = _datasets_from(env.cfg)
    assert data["ds_local"]["SELF"] == "/local/p"   # local always wins for self host


# --------------------------------------------------------------------------- #
# 6. Foreign-host LWW
# --------------------------------------------------------------------------- #
def test_foreign_host_lww_remote_newer(env):
    _write_config(env.cfg, {"shared": {"FOREIGN": "/old"}})
    env.fake.seed({
        "schema_version": 1, "datasets": {"shared": {"FOREIGN": "/new"}},
        "entry_meta": {"shared/FOREIGN": "2999-01-01T00:00:00+00:00"},
    })
    _sync(env)
    # local has no sidecar timestamp -> remote adopted
    assert _datasets_from(env.cfg)["shared"]["FOREIGN"] == "/new"


# --------------------------------------------------------------------------- #
# 7. First push
# --------------------------------------------------------------------------- #
def test_first_push_empty_remote(env):
    assert env.fake.stored() is None
    r = _sync(env)
    assert r["pushed"] is True
    assert env.fake.stored()["datasets"] == {"ds_local": {"SELF": "/local/p"}}


# --------------------------------------------------------------------------- #
# 8. 412 retry with re-expand (preserves concurrent remote add)
# --------------------------------------------------------------------------- #
def test_412_retry_preserves_remote_entry(env, monkeypatch):
    env.fake.seed({"schema_version": 1, "datasets": {"ds_remote": {"B": "/r"}}})
    real_put = env.fake.put_object
    calls = {"n": 0}

    def flaky_put(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            # simulate a concurrent writer landing a new entry D, then 412
            env.fake.seed({
                "schema_version": 1,
                "datasets": {"ds_remote": {"B": "/r"}, "ds_D": {"D": "/d"}},
            })
            raise FakeClientError("PreconditionFailed")
        return real_put(**kw)

    monkeypatch.setattr(env.fake, "put_object", flaky_put)
    r = _sync(env)
    assert r["pushed"] is True
    stored = env.fake.stored()
    assert "ds_D" in stored["datasets"]        # concurrent add preserved
    assert "ds_local" in stored["datasets"]


# --------------------------------------------------------------------------- #
# 9. files LWW
# --------------------------------------------------------------------------- #
def test_files_lww_remote_newer(env):
    (env.tmp / "models.toml").write_text("v_local", encoding="utf-8")
    import os
    old = config_share._iso_from_mtime(0)
    os.utime(env.tmp / "models.toml", (0, 0))   # local very old
    env.fake.seed({
        "schema_version": 1, "datasets": {},
        "files": {"models.toml": "v_remote"},
        "file_meta": {"models.toml": "2100-01-01T00:00:00+00:00"},
    })
    _sync(env)
    assert (env.tmp / "models.toml").read_text(encoding="utf-8") == "v_remote"
    # mtime corrected to ~remote logical time (year 2100)
    import datetime
    mt = datetime.datetime.fromtimestamp(
        (env.tmp / "models.toml").stat().st_mtime, tz=datetime.timezone.utc)
    assert mt.year == 2100
    assert old  # silence lint


def test_files_lww_local_newer_pushes_real_mtime(env):
    import os
    (env.tmp / "models.toml").write_text("v_local", encoding="utf-8")
    os.utime(env.tmp / "models.toml", (1000, 1000))
    env.fake.seed({
        "schema_version": 1, "datasets": {},
        "files": {"models.toml": "v_remote"},
        "file_meta": {"models.toml": "1970-01-01T00:00:00+00:00"},
    })
    r = _sync(env)
    assert r["pushed"] is True
    stored = env.fake.stored()
    assert stored["files"]["models.toml"] == "v_local"
    # pushed meta = real st_mtime (1000), not now
    pushed_meta = config_share._parse_iso(stored["file_meta"]["models.toml"])
    assert abs(pushed_meta.timestamp() - 1000) < 2


def test_files_identical_no_write(env):
    (env.tmp / "models.toml").write_text("same", encoding="utf-8")
    env.fake.seed({
        "schema_version": 1, "datasets": {"ds_local": {"SELF": "/local/p"}},
        "files": {"models.toml": "same"},
    })
    r = _sync(env)
    assert "models.toml" not in r["planned"]["write_files"]


# --------------------------------------------------------------------------- #
# 10. Unknown schema / corrupt remote -> read-only
# --------------------------------------------------------------------------- #
def test_unknown_schema_readonly(env):
    env.fake.seed({"schema_version": 2, "datasets": {"x": {"H": "/p"}}})
    before = env.fake.stored()
    r = _sync(env, apply=False)
    assert r["unusable_remote"] is True
    assert r["pushed"] is False
    assert r["planned"]["would_push"] is False
    assert env.fake.stored() == before          # remote untouched
    assert _datasets_from(env.cfg) == {"ds_local": {"SELF": "/local/p"}}


def test_corrupt_remote_readonly(env):
    env.fake.seed({"schema_version": 1, "datasets": {"x": {"H": 123}}})  # bad type
    r = _sync(env)
    assert r["unusable_remote"] is True
    assert r["pushed"] is False


# --------------------------------------------------------------------------- #
# 11. config-push sidecar invariant
# --------------------------------------------------------------------------- #
def test_push_does_not_update_sidecar_for_foreign(env):
    _write_config(env.cfg, {"shared": {"FOREIGN": "/old"}})
    env.fake.seed({
        "schema_version": 1, "datasets": {"shared": {"FOREIGN": "/new"}},
        "entry_meta": {"shared/FOREIGN": "2999-01-01T00:00:00+00:00"},
    })
    config_share.push(config_path=env.cfg)
    # local config.py unchanged (push never writes local)
    assert _datasets_from(env.cfg)["shared"]["FOREIGN"] == "/old"
    state = config_share._local_state()
    # entry_meta for foreign not adopted as a "local confirmation"
    assert state["entry_meta"].get("shared/FOREIGN") != "2999-01-01T00:00:00+00:00"
    # subsequent both-sync adopts remote new path, does not push back old
    config_share.sync(direction="both", config_path=env.cfg)
    assert _datasets_from(env.cfg)["shared"]["FOREIGN"] == "/new"


# --------------------------------------------------------------------------- #
# 12. tie-break convergence
# --------------------------------------------------------------------------- #
def test_tie_break_datasets(env):
    ts = "2026-01-01T00:00:00+00:00"
    _write_config(env.cfg, {"shared": {"FOREIGN": "/aaa"}})
    # seed local sidecar so local has a timestamp equal to remote's
    config_share._save_local_state(
        {"entry_meta": {"shared/FOREIGN": ts}, "etag": None})
    env.fake.seed({
        "schema_version": 1, "datasets": {"shared": {"FOREIGN": "/zzz"}},
        "entry_meta": {"shared/FOREIGN": ts},
    })
    _sync(env)
    # _tie_break("/aaa", "/zzz") -> False -> remote ("/zzz") wins, deterministic
    assert _datasets_from(env.cfg)["shared"]["FOREIGN"] == "/zzz"


def test_tie_break_files_writes(env):
    import os
    ts = "2026-01-01T00:00:00+00:00"
    epoch = config_share._parse_iso(ts).timestamp()
    (env.tmp / "models.toml").write_text("aaa", encoding="utf-8")
    os.utime(env.tmp / "models.toml", (epoch, epoch))
    env.fake.seed({
        "schema_version": 1, "datasets": {},
        "files": {"models.toml": "zzz"}, "file_meta": {"models.toml": ts},
    })
    _sync(env)
    # remote "zzz" >= local "aaa" -> remote wins, file written
    assert (env.tmp / "models.toml").read_text(encoding="utf-8") == "zzz"


# --------------------------------------------------------------------------- #
# 13. .env path
# --------------------------------------------------------------------------- #
def test_env_written_to_pulled(env):
    env.fake.seed({
        "schema_version": 1, "datasets": {},
        "files": {".env": "SECRET=remote"},
        "file_meta": {".env": "2999-01-01T00:00:00+00:00"},
    })
    config_share.sync(direction="pull", config_path=env.cfg)
    assert (env.tmp / ".env.pulled").read_text(encoding="utf-8") == "SECRET=remote"
    assert not (env.tmp / ".env").exists()


# --------------------------------------------------------------------------- #
# 14. prefix normalization
# --------------------------------------------------------------------------- #
def test_prefix_normalization(env, monkeypatch):
    monkeypatch.setenv("R2_PREFIX", "config")
    captured = {}
    orig_get = env.fake.get_object

    def spy_get(Bucket, Key):
        captured["key"] = Key
        return orig_get(Bucket=Bucket, Key=Key)

    monkeypatch.setattr(env.fake, "get_object", spy_get)
    _sync(env)
    assert captured["key"] == "config/bundle.json"


# --------------------------------------------------------------------------- #
# 15. direction & dry-run
# --------------------------------------------------------------------------- #
def test_pull_does_not_push(env):
    env.fake.seed({"schema_version": 1, "datasets": {"ds_remote": {"B": "/r"}}})
    before = env.fake.stored()
    config_share.sync(direction="pull", config_path=env.cfg)
    assert env.fake.stored() == before          # remote not written
    assert "ds_remote" in _datasets_from(env.cfg)


def test_push_does_not_write_local(env):
    env.fake.seed({"schema_version": 1, "datasets": {"ds_remote": {"B": "/r"}}})
    config_share.sync(direction="push", config_path=env.cfg)
    assert "ds_remote" not in _datasets_from(env.cfg)


def test_dry_run_writes_nothing_but_plans(env):
    env.fake.seed({"schema_version": 1, "datasets": {"ds_remote": {"B": "/r"}}})
    before_remote = env.fake.stored()
    r = config_share.sync(apply=False, config_path=env.cfg)
    assert env.fake.stored() == before_remote
    assert _datasets_from(env.cfg) == {"ds_local": {"SELF": "/local/p"}}
    assert r["planned"]["would_push"] is True
    assert r["planned"]["write_datasets"] is True


def test_dry_run_no_diff(env):
    env.fake.seed({
        "schema_version": 1, "datasets": {"ds_local": {"SELF": "/local/p"}},
    })
    r = config_share.sync(apply=False, config_path=env.cfg)
    assert r["planned"]["would_push"] is False


# --------------------------------------------------------------------------- #
# 16. lost update prevention (registry_transaction reads fresh)
# --------------------------------------------------------------------------- #
def test_lost_update_prevention(env, monkeypatch):
    # config.py on disk has {A, B}; make_bundle is forced stale to {A}
    _write_config(env.cfg, {"A": {"SELF": "/a"}, "B": {"SELF": "/b"}})
    real_make = config_share.make_bundle

    def stale_make(**kw):
        b = real_make(**kw)
        b["datasets"] = {"A": {"SELF": "/a"}}    # drop B
        return b

    monkeypatch.setattr(config_share, "make_bundle", stale_make)
    env.fake.seed({"schema_version": 1, "datasets": {"C": {"D": "/c"}}})
    _sync(env)
    data = _datasets_from(env.cfg)
    assert set(data) == {"A", "B", "C"}          # B not lost


# --------------------------------------------------------------------------- #
# 17. no None in meta
# --------------------------------------------------------------------------- #
def test_no_null_in_meta(env):
    (env.tmp / "models.toml").write_text("local", encoding="utf-8")
    # remote has datasets+files but omits entry_meta/file_meta
    env.fake.seed({
        "schema_version": 1, "datasets": {"ds_remote": {"B": "/r"}},
        "files": {"models.toml": "local"},
    })
    _sync(env)
    stored = env.fake.stored()
    assert None not in stored["entry_meta"].values()
    assert all(isinstance(v, str) for v in stored["entry_meta"].values())
    assert all(isinstance(v, str) for v in stored["file_meta"].values())
    # self-validation: stored bundle must still be usable
    client = config_share._client(None)
    bundle, _etag, unusable = config_share._get_remote(client, "b", "k")
    assert unusable is False


# --------------------------------------------------------------------------- #
# 18. None default wiring (registry_path redirected, real registry untouched)
# --------------------------------------------------------------------------- #
def test_none_default_uses_registry_path(env, monkeypatch):
    import dataset_registry

    monkeypatch.setattr(dataset_registry, "registry_path", lambda: env.cfg)
    b = config_share.make_bundle()              # no config_path
    assert b["datasets"] == {"ds_local": {"SELF": "/local/p"}}
    r = config_share.sync()                     # no config_path
    assert r["pushed"] is True


# --------------------------------------------------------------------------- #
# 19. files whitelist & path safety
# --------------------------------------------------------------------------- #
def test_whitelist_and_path_safety(env):
    env.fake.seed({
        "schema_version": 1, "datasets": {},
        "files": {"../escape": "x", "/tmp/pwn": "y",
                  "common/paths.py": "z", "models.toml": "ok"},
        "file_meta": {"models.toml": "2999-01-01T00:00:00+00:00"},
    })
    config_share.sync(direction="pull", config_path=env.cfg)
    assert (env.tmp / "models.toml").read_text(encoding="utf-8") == "ok"
    assert not (env.tmp / "common" / "paths.py").exists()
    assert not (env.tmp.parent / "escape").exists()


def test_safe_target_unit(env):
    root = env.tmp
    assert config_share._safe_target("../x", root) is None
    assert config_share._safe_target("/abs", root) is None
    assert config_share._safe_target("a/../../b", root) is None
    assert config_share._safe_target(".env", root) == root.resolve() / ".env.pulled"
    assert config_share._safe_target("models.toml", root) == root.resolve() / "models.toml"


# --------------------------------------------------------------------------- #
# 20. R2 cleanup of dangerous keys
# --------------------------------------------------------------------------- #
def test_cleanup_dangerous_keys(env):
    env.fake.seed({
        "schema_version": 1, "datasets": {"ds_local": {"SELF": "/local/p"}},
        "files": {"/tmp/pwn": "y"},
    })
    r = config_share.sync(direction="both", config_path=env.cfg)
    assert r["pushed"] is True
    stored = env.fake.stored()
    assert "/tmp/pwn" not in stored["files"]     # scrubbed


def test_cleanup_pull_does_not_write_remote(env):
    seed = {
        "schema_version": 1, "datasets": {"ds_local": {"SELF": "/local/p"}},
        "files": {"/tmp/pwn": "y"},
    }
    env.fake.seed(seed)
    before = env.fake.stored()
    config_share.sync(direction="pull", config_path=env.cfg)
    assert env.fake.stored() == before


# --------------------------------------------------------------------------- #
# 21. portable file lost-update guard (both directions)
# --------------------------------------------------------------------------- #
def test_stale_upload_prevented(env):
    # make_bundle reads v1; the loop-start fresh read picks up v3 before PUT,
    # so the uploaded content is the latest local edit (not the stale snapshot).
    (env.tmp / "models.toml").write_text("v1", encoding="utf-8")
    config_share.make_bundle(config_path=env.cfg)   # reads v1
    (env.tmp / "models.toml").write_text("v3", encoding="utf-8")
    r = _sync(env)
    assert r["pushed"] is True
    assert env.fake.stored()["files"]["models.toml"] == "v3"


def test_put_time_stale_upload_remerged(env, monkeypatch):
    # local->remote: 編集が「ループ冒頭の collect の後・put の前」に入るケース。
    # put 直前 recheck がこれを検知して再ループし、最新 v2 を push する。
    (env.tmp / "models.toml").write_text("v1", encoding="utf-8")
    real_collect = config_share._collect_portable_files
    calls = {"n": 0}

    def collect_then_edit(**kw):
        res = real_collect(**kw)
        calls["n"] += 1
        # 1=make_bundle, 2=loop1 冒頭。loop1 冒頭の直後に v2 へ編集 → put 直前 recheck(3)
        # が v2 を見て再ループ → loop2 で v2 を再マージして push する。
        if calls["n"] == 2:
            (env.tmp / "models.toml").write_text("v2", encoding="utf-8")
        return res

    monkeypatch.setattr(config_share, "_collect_portable_files", collect_then_edit)
    r = _sync(env)
    assert r["pushed"] is True
    assert env.fake.stored()["files"]["models.toml"] == "v2"


def test_remote_stale_overwrite_prevented(env, monkeypatch):
    import os
    (env.tmp / "models.toml").write_text("v1_local", encoding="utf-8")
    os.utime(env.tmp / "models.toml", (0, 0))
    env.fake.seed({
        "schema_version": 1, "datasets": {},
        "files": {"models.toml": "v2_remote"},
        "file_meta": {"models.toml": "2999-01-01T00:00:00+00:00"},
    })
    real_read = config_share._read_file_state
    calls = {"n": 0}

    def read_then_swap(p):
        kind, text, mtime = real_read(p)
        # 1=make_bundle collect, 2=loop collect, 3=write-time recheck.
        # Simulate a concurrent edit right after the loop-start collect (2),
        # so the write-time recheck (3) sees different content and skips.
        if p.name == "models.toml":
            calls["n"] += 1
            if calls["n"] == 2:
                p.write_text("v3_concurrent", encoding="utf-8")
        return kind, text, mtime

    monkeypatch.setattr(config_share, "_read_file_state", read_then_swap)
    r = _sync(env)
    assert (env.tmp / "models.toml").read_text(encoding="utf-8") == "v3_concurrent"
    assert any("skip" in w or "保持" in w for w in r["warnings"])


# --------------------------------------------------------------------------- #
# 22. unreadable file protection / 3-state
# --------------------------------------------------------------------------- #
def test_read_file_state_three_way(env):
    p = env.tmp / "x.txt"
    assert config_share._read_file_state(p) == ("missing", None, None)
    p.write_bytes(b"\xff\xfe\x00")
    assert config_share._read_file_state(p)[:2] == ("unreadable", None)
    p.write_text("ok", encoding="utf-8")
    kind, text, mtime = config_share._read_file_state(p)
    assert (kind, text) == ("text", "ok")
    assert isinstance(mtime, float)


def test_unreadable_local_not_overwritten(env):
    (env.tmp / "models.toml").write_bytes(b"\xff\xfe\x00bad")
    env.fake.seed({
        "schema_version": 1, "datasets": {},
        "files": {"models.toml": "v_remote"},
        "file_meta": {"models.toml": "2999-01-01T00:00:00+00:00"},
    })
    r = _sync(env)
    assert (env.tmp / "models.toml").read_bytes() == b"\xff\xfe\x00bad"
    assert any("保持" in w or "読込不能" in w for w in r["warnings"])


def test_write_time_unreadable_skipped(env, monkeypatch):
    # remote-only file, missing at loop start, becomes unreadable right before write
    env.fake.seed({
        "schema_version": 1, "datasets": {},
        "files": {"llm_backend/config.toml": "remote"},
        "file_meta": {"llm_backend/config.toml": "2999-01-01T00:00:00+00:00"},
    })
    real_read = config_share._read_file_state
    calls = {"n": 0}

    def read_then_corrupt(p):
        kind, text, mtime = real_read(p)
        if p.name == "config.toml" and kind == "missing":
            calls["n"] += 1
            (p.parent).mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\xff\xfe\x00")     # now unreadable
        return kind, text, mtime

    monkeypatch.setattr(config_share, "_read_file_state", read_then_corrupt)
    r = _sync(env)
    # file stays unreadable bytes, not overwritten by remote text
    assert (env.tmp / "llm_backend" / "config.toml").read_bytes() == b"\xff\xfe\x00"
    assert any("読込不能" in w or "保持" in w for w in r["warnings"])


def test_missing_remote_only_created(env):
    assert not (env.tmp / "llm_backend" / "config.toml").exists()
    env.fake.seed({
        "schema_version": 1, "datasets": {},
        "files": {"llm_backend/config.toml": "remote"},
        "file_meta": {"llm_backend/config.toml": "2999-01-01T00:00:00+00:00"},
    })
    config_share.sync(direction="pull", config_path=env.cfg)
    assert (env.tmp / "llm_backend" / "config.toml").read_text(encoding="utf-8") == "remote"


# --------------------------------------------------------------------------- #
# 25. registry JSON 化に伴う追加ケース（Issue #95）
# --------------------------------------------------------------------------- #
def test_pull_creates_absent_local_registry(env):
    """ローカル未作成でも既存 schema の bundle から復元し、JSON を作る。"""
    env.cfg.unlink()
    env.fake.seed({
        "schema_version": 1,
        "updated_by": "OTHER", "updated_at": "2026-01-01T00:00:00+00:00",
        "datasets": {"ds_remote": {"OTHER": "/r/p"}},
        "entry_meta": {"ds_remote/OTHER": "2026-01-01T00:00:00+00:00"},
        "files": {}, "file_meta": {},
    })
    config_share.sync(direction="pull", config_path=env.cfg)
    assert env.cfg.exists()
    assert _datasets_from(env.cfg) == {"ds_remote": {"OTHER": "/r/p"}}
    config.reload_datasets(config_path=env.cfg)
    assert "ds_remote" in config.DATASETS


def test_empty_both_sides_creates_nothing(env):
    env.cfg.unlink()
    env.fake.seed({
        "schema_version": 1, "datasets": {}, "entry_meta": {},
        "files": {}, "file_meta": {},
        "updated_by": "OTHER", "updated_at": "2026-01-01T00:00:00+00:00",
    })
    config_share.sync(direction="pull", config_path=env.cfg)
    assert not env.cfg.exists()


def test_corrupt_local_registry_blocks_sync(env):
    """ローカル破損時は同期を失敗させ、remote 上書き・空 push・ローカル上書きをしない。"""
    env.cfg.write_text("{broken", encoding="utf-8")
    env.fake.seed({
        "schema_version": 1, "datasets": {"ds_remote": {"OTHER": "/r/p"}},
        "entry_meta": {}, "files": {}, "file_meta": {},
        "updated_by": "OTHER", "updated_at": "2026-01-01T00:00:00+00:00",
    })
    remote_before = env.fake.stored()
    with pytest.raises(config.RegistryError):
        config_share.sync(direction="both", config_path=env.cfg)
    assert env.cfg.read_text(encoding="utf-8") == "{broken"
    assert env.fake.stored() == remote_before


def test_corrupt_local_registry_try_sync_swallows(env):
    env.cfg.write_text("{broken", encoding="utf-8")
    assert config_share.try_sync(config_path=env.cfg) is None


def test_dry_run_leaves_registry_state_and_remote_untouched(env):
    before = env.cfg.read_bytes()
    env.fake.seed({
        "schema_version": 1, "datasets": {"ds_remote": {"OTHER": "/r/p"}},
        "entry_meta": {}, "files": {}, "file_meta": {},
        "updated_by": "OTHER", "updated_at": "2026-01-01T00:00:00+00:00",
    })
    remote_before = env.fake.stored()
    state_path = env.tmp / "state.json"
    state_before = state_path.read_bytes() if state_path.exists() else None
    config_share.sync(apply=False, config_path=env.cfg)
    assert env.cfg.read_bytes() == before
    assert env.fake.stored() == remote_before
    assert (state_path.read_bytes() if state_path.exists() else None) == state_before
