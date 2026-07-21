"""Tests for llm_bridge.dataset_meta (Qt-free core).

config.DATASETS / config.get_dataset_dir are monkeypatched to tmp dirs. These
cover compute_meta LIGHT/HEAVY split, never-raise guards, _merge_meta three-state
semantics, _is_stale, from_dict type normalization, rebuild_meta lock ordering,
patch_description, load_one overlay, and MRU type normalization.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import config
import dataset_config
from llm_bridge import dataset_meta
from llm_bridge.dataset_meta import (
    DatasetMeta,
    META_VERSION,
    _is_stale,
    _merge_meta,
)


@pytest.fixture()
def ds_env(monkeypatch, tmp_path):
    """One dataset 'ds' → tmp dir, with a _work dir + toml."""
    d = tmp_path / "ds"
    (d / "_work").mkdir(parents=True)
    (d / "myanalysis.toml").write_text(
        'work_dir = "_work"\nformat = "csv_per_subdir"\n', encoding="utf-8")
    mapping = {"ds": d}
    monkeypatch.setattr(config, "DATASETS", {"ds": {"THISHOST": str(d)}})
    monkeypatch.setattr(config, "get_dataset_dir", lambda name: mapping[name])
    monkeypatch.setattr(config, "reload_datasets", lambda config_path=None: None)
    return d


def _make_analysis(dataset_dir, name):
    ad = dataset_dir / "analyses" / name
    ad.mkdir(parents=True)
    (ad / "analysis.py").write_text("# analysis\n", encoding="utf-8")


# ---- compute_meta ----

def test_compute_heavy_full(ds_env):
    _make_analysis(ds_env, "a1")
    _make_analysis(ds_env, "a2")
    m = dataset_meta.compute_meta("ds", heavy=True)
    assert m["analysis_count"] == 2
    assert sorted(m["analysis_names"]) == ["a1", "a2"]
    assert "disk_size_bytes" in m
    assert m["format"] == "csv_per_subdir"


def test_compute_light_has_no_heavy_fields(ds_env):
    _make_analysis(ds_env, "a1")
    m = dataset_meta.compute_meta("ds", heavy=False)
    for f in dataset_meta.HEAVY_FIELDS:
        assert f not in m
    assert m["analysis_count"] == 1


def test_compute_read_only_no_writes(ds_env):
    _make_analysis(ds_env, "a1")
    before = set(p.name for p in (ds_env / "_work").iterdir())
    dataset_meta.compute_meta("ds", heavy=True)
    after = set(p.name for p in (ds_env / "_work").iterdir())
    assert before == after
    # no state/batch trees created
    assert not (ds_env / "_work" / "analyses").exists()


def test_compute_corrupt_toml_still_gets_analysis_count(ds_env, monkeypatch):
    _make_analysis(ds_env, "a1")
    import tomllib

    def boom(name, *, create=True):
        raise tomllib.TOMLDecodeError("bad")
    monkeypatch.setattr(dataset_config, "get_work_dir", boom)
    m = dataset_meta.compute_meta("ds", heavy=True)
    assert m["analysis_count"] == 1        # dataset_dir-derived, still works
    assert "annotation_total" not in m     # work_dir=None → skipped (no exc leak)
    assert "export_png_count" not in m


def test_compute_corrupt_annotations_no_key(ds_env):
    _make_analysis(ds_env, "a1")
    sd = dataset_config.state_dir("ds", "a1", create=True)
    (sd / "annotations.json").write_text("{bad json", encoding="utf-8")
    m = dataset_meta.compute_meta("ds", heavy=True)
    # one corrupt analysis → its contribution 0, no raise; key present with 0
    assert m.get("annotation_total") == 0


def test_should_stop_cancels_heavy(ds_env):
    _make_analysis(ds_env, "a1")
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] >= 1     # stop immediately
    m = dataset_meta.compute_meta("ds", heavy=True, should_stop=stop)
    assert m.get("_heavy_cancelled") is True
    assert "disk_size_bytes" not in m


def test_walk_permission_error_no_raise(ds_env, monkeypatch):
    _make_analysis(ds_env, "a1")

    def boom(*a, **k):
        raise PermissionError("nope")
    monkeypatch.setattr(dataset_meta.os, "walk", boom)
    m = dataset_meta.compute_meta("ds", heavy=True)   # must not raise
    assert "disk_size_bytes" not in m


def test_disk_size_includes_work_but_last_meas_excludes(ds_env):
    import os as _os

    _make_analysis(ds_env, "a1")
    raw = ds_env / "raw.csv"
    raw.write_bytes(b"x" * 100)
    work_png = ds_env / "_work" / "big.png"
    work_png.write_bytes(b"y" * 1000)
    # analysis.py under <dataset_dir>/analyses/ must also be excluded from
    # last_measurement even though it is the newest file on disk.
    ana_py = ds_env / "analyses" / "a1" / "analysis.py"
    # Distinct mtimes: raw.csv OLD, _work/big.png and analyses/a1/analysis.py NEW.
    old_t, new_t = 1_000_000.0, 2_000_000.0
    _os.utime(raw, (old_t, old_t))
    _os.utime(work_png, (new_t, new_t))
    _os.utime(ana_py, (new_t, new_t))

    m = dataset_meta.compute_meta("ds", heavy=True)
    assert m["disk_size_bytes"] >= 1100          # _work counted in disk_size
    # last_measurement must equal raw.csv's mtime, NOT the newer _work/analyses
    # files — a regression in the exclusion predicate would leak new_t here.
    assert m["last_measurement"] == old_t


def test_chat_session_count_globs(ds_env):
    cs = ds_env / "_work" / "chat_sessions"
    cs.mkdir()
    (cs / "a.json").write_text("{}", encoding="utf-8")
    (cs / "b.json").write_text("{}", encoding="utf-8")
    m = dataset_meta.compute_meta("ds", heavy=False)
    assert m["chat_session_count"] == 2


def test_external_symlink_analysis_excluded(ds_env, tmp_path):
    import os
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "analysis.py").write_text("# x\n", encoding="utf-8")
    root = ds_env / "analyses"
    root.mkdir()
    try:
        os.symlink(outside, root / "evil")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported")
    m = dataset_meta.compute_meta("ds", heavy=False)
    assert "evil" not in m.get("analysis_names", [])


# ---- _merge_meta ----

def test_merge_light_missing_dropped():
    existing = {"analysis_count": 5}
    merged = _merge_meta(existing, {}, heavy=False)
    assert "analysis_count" not in merged


def test_merge_heavy_kept_on_light_update():
    existing = {"disk_size_bytes": 999}
    merged = _merge_meta(existing, {"analysis_count": 1}, heavy=False)
    assert merged["disk_size_bytes"] == 999


def test_merge_heavy_dropped_on_completed_failure():
    existing = {"disk_size_bytes": 999}
    merged = _merge_meta(existing, {}, heavy=True)
    assert "disk_size_bytes" not in merged


def test_merge_heavy_kept_on_cancel():
    existing = {"disk_size_bytes": 999}
    merged = _merge_meta(existing, {"_heavy_cancelled": True}, heavy=True)
    assert merged["disk_size_bytes"] == 999


# ---- rebuild_meta ----

def test_rebuild_preserves_description(ds_env):
    dataset_meta.patch_description("ds", "hello")
    dataset_meta.rebuild_meta("ds", heavy=False)
    assert dataset_meta.read_meta("ds")["description"] == "hello"


def test_rebuild_compute_outside_lock(ds_env, monkeypatch):
    order = []
    real_lock = dataset_meta.exclusive_lock

    import contextlib

    @contextlib.contextmanager
    def spy_lock(path):
        order.append("lock")
        with real_lock(path):
            yield

    def spy_compute(dataset, *, heavy=True, should_stop=None):
        order.append("compute")
        return {"analysis_count": 0, "analysis_names": []}

    monkeypatch.setattr(dataset_meta, "exclusive_lock", spy_lock)
    monkeypatch.setattr(dataset_meta, "compute_meta", spy_compute)
    dataset_meta.rebuild_meta("ds", heavy=False)
    assert order.index("compute") < order.index("lock")


# ---- patch_description ----

def test_patch_description_no_updated_at_and_preserves(ds_env):
    dataset_meta.rebuild_meta("ds", heavy=False)
    before = dataset_meta.read_meta("ds")
    dataset_meta.patch_description("ds", "desc")
    after = dataset_meta.read_meta("ds")
    assert after["description"] == "desc"
    assert after.get("updated_at") == before.get("updated_at")   # not bumped
    assert after["analysis_count"] == before["analysis_count"]


def test_patch_description_from_scratch_is_stale(ds_env):
    dataset_meta.patch_description("ds", "x")
    m = dataset_meta.read_meta("ds")
    assert "analysis_count" not in m
    assert _is_stale(m) is True


# ---- _is_stale ----

@pytest.mark.parametrize("meta", [
    None,
    {"version": 999, "analysis_count": 1},
    {"version": META_VERSION},                       # missing analysis_count
    {"version": META_VERSION, "analysis_count": []}, # wrong type
    {"version": True, "analysis_count": 1},          # bool version
])
def test_is_stale_true(meta):
    assert _is_stale(meta) is True


def test_is_stale_false():
    assert _is_stale({"version": META_VERSION, "analysis_count": 3}) is False


# ---- read_meta ----

def test_read_meta_binary_returns_none(ds_env):
    (ds_env / "meta.json").write_bytes(b"\xff\xfe\x00\x01binary")
    assert dataset_meta.read_meta("ds") is None


def test_read_meta_non_dict_none(ds_env):
    (ds_env / "meta.json").write_text("[1,2]", encoding="utf-8")
    assert dataset_meta.read_meta("ds") is None


# ---- durability: description survives transient/lost meta.json (mount) ----

def test_write_meta_creates_bak(ds_env):
    dataset_meta.patch_description("ds", "hi")
    bak = ds_env / "meta.json.bak"
    assert bak.is_file()
    assert json.loads(bak.read_text(encoding="utf-8"))["description"] == "hi"


def test_rebuild_bails_on_unreadable_meta_preserving_description(ds_env, monkeypatch):
    dataset_meta.patch_description("ds", "IMPORTANT-KEEP")
    saved = (ds_env / "meta.json").read_bytes()   # read_bytes は patch されない
    real_read_text = Path.read_text
    metas = {str(ds_env / "meta.json"), str(ds_env / "meta.json.bak")}

    def blind(self, *a, **k):
        if str(self) in metas:
            raise OSError("mount 1005")   # primary も .bak も読めない
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", blind)
    dataset_meta.rebuild_meta("ds", heavy=False)   # unreadable → bail（上書きしない）
    # read_bytes は patch されないので、undo せずに（ds_env の patch を壊さずに）検証する。
    after = (ds_env / "meta.json").read_bytes()
    assert after == saved
    assert json.loads(after.decode("utf-8"))["description"] == "IMPORTANT-KEEP"


def test_rebuild_recovers_description_from_bak_when_primary_zeroed(ds_env):
    dataset_meta.patch_description("ds", "keepme")
    (ds_env / "meta.json").write_bytes(b"")        # primary evicted → 0byte, .bak 健全
    dataset_meta.rebuild_meta("ds", heavy=False)   # durable_read が .bak から回復して保持
    assert dataset_meta.read_meta("ds")["description"] == "keepme"
    assert (ds_env / "meta.json").stat().st_size > 0   # rebuild の書込で primary が healed


def test_rebuild_writes_fresh_when_absent(ds_env):
    _make_analysis(ds_env, "a1")
    assert not (ds_env / "meta.json").exists()
    dataset_meta.rebuild_meta("ds", heavy=False)
    m = dataset_meta.read_meta("ds")
    assert m["description"] == ""
    assert m["analysis_count"] == 1


def test_last_measurement_ignores_meta_bak(ds_env):
    # meta.json.bak は測定ファイルではない → last_measurement を汚さないこと。
    _make_analysis(ds_env, "a1")
    meas = ds_env / "raw.csv"
    meas.write_text("x\n", encoding="utf-8")
    old = 1_000_000.0
    os.utime(meas, (old, old))
    dataset_meta.patch_description("ds", "d")       # meta.json(.bak) を now で書く
    m = dataset_meta.compute_meta("ds", heavy=True)
    assert m["last_measurement"] == pytest.approx(old)   # now(meta.bak) ではなく raw.csv


# ---- from_dict ----

def test_from_dict_unknown_keys_ignored():
    m = DatasetMeta.from_dict({"bogus": 1, "analysis_count": 3})
    assert m.analysis_count == 3


def test_from_dict_bad_numeric_dropped():
    m = DatasetMeta.from_dict({"last_touched": "bad", "analysis_count": []})
    assert m.last_touched is None
    assert m.analysis_count is None


def test_from_dict_bool_rejected_for_numeric():
    m = DatasetMeta.from_dict({"analysis_count": True})
    assert m.analysis_count is None


def test_from_dict_list_element_filter():
    m = DatasetMeta.from_dict({"analysis_names": [1, "a", {}]})
    assert m.analysis_names == ["a"]


def test_from_dict_ignores_name():
    m = DatasetMeta.from_dict({"name": "sneaky"})
    assert m.name is None


# ---- load_one / load_for_picker ----

def test_load_one_sets_name_and_availability(ds_env):
    dataset_meta.rebuild_meta("ds", heavy=False)
    m = dataset_meta.load_one("ds")
    assert m.name == "ds"
    assert m.available is True


def test_load_one_no_host(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATASETS", {"ds": {}})

    def raise_rt(name):
        raise RuntimeError("no host")
    monkeypatch.setattr(config, "get_dataset_dir", raise_rt)
    m = dataset_meta.load_one("ds")
    assert m.available is False
    assert m.unavailable_reason == "no-host"
    assert m.name == "ds"


def test_load_for_picker_one_corrupt_does_not_stop(ds_env, monkeypatch):
    monkeypatch.setattr(config, "DATASETS",
                        {"ds": {"H": str(ds_env)}, "unknown": {}})

    real = config.get_dataset_dir

    def gdd(name):
        if name == "unknown":
            raise KeyError("unknown")
        return real(name)
    monkeypatch.setattr(config, "get_dataset_dir", gdd)
    metas = dataset_meta.load_for_picker(["ds", "unknown"])
    assert len(metas) == 2
    assert {m.name for m in metas} == {"ds", "unknown"}


def test_type_broken_meta_sortable(ds_env):
    (ds_env / "meta.json").write_text(
        json.dumps({"version": 1, "last_touched": "bad", "analysis_count": []}),
        encoding="utf-8")
    m = dataset_meta.load_one("ds")
    # from_dict dropped the bad values → None → sort keys are safe
    key = (-(m.last_opened if m.last_opened is not None else float("-inf")),
           -(m.last_touched or 0.0), m.name or "")
    assert key[2] == "ds"


# ---- concurrent patch_description ----

def test_concurrent_patch_no_loss(ds_env):
    import threading
    results = []

    def worker(text):
        dataset_meta.patch_description("ds", text)
        results.append(text)
    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    m = dataset_meta.read_meta("ds")
    assert m["description"].startswith("t")   # some final value, not corrupted
    assert len(results) == 5


# ---- thumbnail ----

def test_thumbnail_resolved_from_active_tab(ds_env):
    _make_analysis(ds_env, "a1")
    sd = dataset_config.state_dir("ds", "a1", create=True)
    (sd / "current_view.png").write_bytes(b"PNG")
    sess = ds_env / "_work" / "session.json"
    sess.write_text(json.dumps({
        "tabs": [{"name": "tab1", "kind": "analysis", "module": "a1"}],
        "active_tab": "tab1",
    }), encoding="utf-8")
    m = dataset_meta.compute_meta("ds", heavy=False)
    assert m["thumbnail"].endswith("current_view.png")
