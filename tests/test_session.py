"""Tests for llm_bridge.session: per-dataset session.json round-trip (Qt-free).

config.DATASETS / config.get_dataset_dir are monkeypatched to tmp dirs so no
real data dir is touched. These exercise write_session/read_session round-trip,
infer_dataset prefix matching (longest match wins), and graceful None on
unregistered host / missing dataset.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import config
import dataset_config
from llm_bridge import session


@pytest.fixture()
def ds_env(monkeypatch, tmp_path):
    """Register two datasets, each mapped to its own tmp dir."""
    a_dir = tmp_path / "ds_a"
    b_dir = tmp_path / "ds_b"
    a_dir.mkdir()
    b_dir.mkdir()
    mapping = {"ds_a": a_dir, "ds_b": b_dir}
    monkeypatch.setattr(config, "DATASETS", {"ds_a": {}, "ds_b": {}})
    monkeypatch.setattr(config, "get_dataset_dir", lambda name: mapping[name])
    monkeypatch.setattr(config, "reload_datasets", lambda config_path=None: None)
    return mapping


# ---- round-trip ----

def test_write_read_roundtrip(ds_env):
    payload = {
        "version": 1,
        "dataset": "ds_a",
        "active_tab": "fig1",
        "tabs": [
            {"name": "fig1", "kind": "figure", "figure": "figures/fig1.png"},
        ],
    }
    session.write_session("ds_a", payload)
    got = session.read_session("ds_a")
    assert got == payload


def test_read_missing_returns_none(ds_env):
    assert session.read_session("ds_a") is None


def test_read_bad_json_returns_none(ds_env):
    work_dir = dataset_config.get_work_dir("ds_a")
    (work_dir / "session.json").write_text("{ not json", encoding="utf-8")
    assert session.read_session("ds_a") is None


def test_read_unknown_dataset_returns_none(ds_env):
    assert session.read_session("nope") is None


# ---- infer_dataset ----

def test_infer_dataset_matches(ds_env):
    work_dir = dataset_config.get_work_dir("ds_a")
    fig = work_dir / "figures" / "x.png"
    fig.parent.mkdir(parents=True, exist_ok=True)
    fig.write_bytes(b"")
    assert session.infer_dataset(str(fig)) == "ds_a"


def test_infer_dataset_no_match_returns_none(ds_env, tmp_path):
    outside = tmp_path / "outside" / "y.png"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"")
    assert session.infer_dataset(str(outside)) is None


def test_infer_dataset_longest_match_wins(monkeypatch, tmp_path):
    """Nested work_dirs: the deeper (longest) one is the correct owner."""
    outer = tmp_path / "outer"
    inner = outer / "nested"
    inner.mkdir(parents=True)
    monkeypatch.setattr(config, "DATASETS", {"outer": {}, "inner": {}})
    monkeypatch.setattr(
        config, "get_dataset_dir",
        lambda name: {"outer": outer, "inner": inner}[name],
    )
    # both use absolute work_dir = the dataset dir itself
    for ds, d in (("outer", outer), ("inner", inner)):
        (d / dataset_config.CONFIG_FILENAME).write_text(
            f'work_dir = {str(d)!r}\n', encoding="utf-8"
        )
    fig = inner / "z.png"
    fig.write_bytes(b"")
    assert session.infer_dataset(str(fig)) == "inner"


def test_infer_dataset_unregistered_host_no_raise(monkeypatch, tmp_path):
    """get_dataset_dir raising for a dataset must not abort inference."""
    good = tmp_path / "good"
    good.mkdir()

    def fake_get_dataset_dir(name):
        if name == "bad":
            raise RuntimeError("no host path")
        return good

    monkeypatch.setattr(config, "DATASETS", {"bad": {}, "good": {}})
    monkeypatch.setattr(config, "get_dataset_dir", fake_get_dataset_dir)
    fig = (good / "_work" / "f.png")
    fig.parent.mkdir(parents=True)
    fig.write_bytes(b"")
    assert session.infer_dataset(str(fig)) == "good"


# ---- save_all _touched lifecycle (Qt-free via a fake window) ----

class _FakeTab:
    def __init__(self, name, spec):
        self.name = name
        self.session_spec = spec


class _FakeWindow:
    def __init__(self, tabs, active=None):
        self._tabs = tabs
        self._active = active
        self.dirty_cleared = False

    def tabs(self):
        return self._tabs

    def active_tab(self):
        return self._active

    def clear_session_dirty(self):
        self.dirty_cleared = True


def test_save_all_partial_failure(ds_env, monkeypatch):
    session._touched.clear()
    spec_a = {"kind": "figure", "name": "fa", "dataset": "ds_a",
              "figure": str(ds_env["ds_a"] / "_work" / "fa.png")}
    spec_b = {"kind": "figure", "name": "fb", "dataset": "ds_b",
              "figure": str(ds_env["ds_b"] / "_work" / "fb.png")}
    tabs = [_FakeTab("fa", spec_a), _FakeTab("fb", spec_b)]
    win = _FakeWindow(tabs, active=tabs[0])

    orig = dataset_config.get_work_dir

    def failing(name):
        if name == "ds_b":
            raise RuntimeError("no host path")
        return orig(name)

    monkeypatch.setattr(dataset_config, "get_work_dir", failing)
    saved, failed = session.save_all(win)
    assert saved == ["ds_a"]
    assert failed == ["ds_b"]
    assert not win.dirty_cleared  # dirty kept on partial failure


def test_save_all_touched_removed_after_empty_save(ds_env):
    session._touched.clear()
    # First: a tab present for ds_a → save.
    spec_a = {"kind": "figure", "name": "fa", "dataset": "ds_a",
              "figure": str(ds_env["ds_a"] / "_work" / "fa.png")}
    win = _FakeWindow([_FakeTab("fa", spec_a)], active=None)
    session.note_dataset("ds_a")
    session.save_all(win)
    assert "ds_a" in session._touched  # kept after non-empty save

    # Then: all tabs closed → re-save writes empty tabs:[] and removes from _touched.
    win2 = _FakeWindow([], active=None)
    saved, failed = session.save_all(win2)
    assert "ds_a" in saved
    assert "ds_a" not in session._touched
    data = session.read_session("ds_a")
    assert data["tabs"] == []


def test_touched_removed_then_unavailable_does_not_block(ds_env, monkeypatch):
    """After _touched removal, ds becoming unavailable shouldn't fail other saves."""
    session._touched.clear()
    spec_a = {"kind": "figure", "name": "fa", "dataset": "ds_a",
              "figure": str(ds_env["ds_a"] / "_work" / "fa.png")}
    win = _FakeWindow([_FakeTab("fa", spec_a)], active=None)
    session.note_dataset("ds_a")
    session.save_all(win)

    # Close all tabs for ds_a → empty save → removed from _touched.
    session.save_all(_FakeWindow([], active=None))
    assert "ds_a" not in session._touched

    # Now make ds_a unavailable.
    orig = dataset_config.get_work_dir
    def failing(name):
        if name == "ds_a":
            raise RuntimeError("host gone")
        return orig(name)
    monkeypatch.setattr(dataset_config, "get_work_dir", failing)

    # Save ds_b only — ds_a not in targets, so no failure.
    spec_b = {"kind": "figure", "name": "fb", "dataset": "ds_b",
              "figure": str(ds_env["ds_b"] / "_work" / "fb.png")}
    win3 = _FakeWindow([_FakeTab("fb", spec_b)], active=None)
    session.note_dataset("ds_b")
    saved, failed = session.save_all(win3)
    assert "ds_b" in saved
    assert failed == []
    assert win3.dirty_cleared


# ---- open_dataset does not pollute _touched ----

class _DispatchWindow(_FakeWindow):
    """Fake window that records dispatch_command calls without executing them."""
    def __init__(self):
        super().__init__([], active=None)
        self._dirty = False
        self._suppress = False
        self.noted_datasets: list[str | None] = []

    def note_current_dataset(self, name):
        self.noted_datasets.append(name)

    def dispatch_command(self, verb, **kwargs):
        pass  # no-op; don't actually try to create tabs

    def is_session_dirty(self):
        return self._dirty

    def set_suppress_dirty(self, b):
        self._suppress = b

    def mark_session_dirty(self):
        if not self._suppress:
            self._dirty = True

    def set_active_tab(self, name):
        return False

    def close_tab(self, name):
        return False


def test_open_dataset_workdir_failure_no_touched(ds_env, monkeypatch):
    """(7a) work_dir resolution failure → error string, _touched not polluted."""
    session._touched.clear()
    monkeypatch.setattr(
        config, "get_dataset_dir",
        lambda name: (_ for _ in ()).throw(RuntimeError("no host")),
    )
    win = _DispatchWindow()
    result = session.open_dataset(win, "ds_a")
    assert result.startswith("error:")
    assert "ds_a" not in session._touched


def test_open_dataset_no_session_no_touched(ds_env):
    """(7b) work_dir OK but no session.json → no-session string, _touched not polluted."""
    session._touched.clear()
    win = _DispatchWindow()
    result = session.open_dataset(win, "ds_a")
    assert result.startswith("no-session:")
    assert "ds_a" not in session._touched


def test_open_dataset_all_figures_missing_no_touched(ds_env):
    """(7c) session exists but all figures missing → restored:0, _touched not polluted."""
    session._touched.clear()
    payload = {
        "version": 1, "dataset": "ds_a", "active_tab": None,
        "tabs": [
            {"name": "gone1", "kind": "figure", "figure": "figures/gone1.png"},
            {"name": "gone2", "kind": "figure", "figure": "figures/gone2.png"},
        ],
    }
    session.write_session("ds_a", payload)
    win = _DispatchWindow()
    result = session.open_dataset(win, "ds_a")
    assert result == "restored:0"
    assert "ds_a" not in session._touched


def test_open_dataset_calls_note_current_dataset(ds_env, monkeypatch):
    """(a) no-session 経路で note_current_dataset が呼ばれる。"""
    import config
    monkeypatch.setattr(config, "reload_datasets", lambda config_path=None: None)
    session._touched.clear()
    win = _DispatchWindow()
    result = session.open_dataset(win, "ds_a")
    assert result.startswith("no-session:")
    assert win.noted_datasets == ["ds_a"]


def test_open_dataset_error_skips_note(ds_env, monkeypatch):
    """(b) error 経路で note_current_dataset が呼ばれない。"""
    import config as config_mod
    monkeypatch.setattr(config_mod, "reload_datasets", lambda config_path=None: None)
    session._touched.clear()
    win = _DispatchWindow()
    # config.get_dataset_dir を差し替えて _resolve_work_dir_readonly を失敗させる。
    # _resolve_work_dir_readonly は config.get_dataset_dir(dataset) を呼ぶ（session.py:51）。
    monkeypatch.setattr(
        config_mod, "get_dataset_dir",
        lambda name: (_ for _ in ()).throw(KeyError(f"unknown: {name}")),
    )
    result = session.open_dataset(win, "ds_a")
    assert result.startswith("error:")
    assert win.noted_datasets == []


class _LegacyWindow:
    """Window without note_current_dataset (pre-upgrade compatibility test).

    Independent class (not inheriting _DispatchWindow) — delattr on inherited
    attributes raises AttributeError.
    """
    def __init__(self):
        self._tabs_list = []
        self._active = None
        self.dirty_cleared = False
        self._dirty = False
        self._suppress = False
        self._cw = None

    def tabs(self):
        return self._tabs_list

    def active_tab(self):
        return self._active

    def clear_session_dirty(self):
        self.dirty_cleared = True

    def dispatch_command(self, verb, **kwargs):
        pass

    def is_session_dirty(self):
        return self._dirty

    def set_suppress_dirty(self, b):
        self._suppress = b

    def mark_session_dirty(self):
        if not self._suppress:
            self._dirty = True

    def set_active_tab(self, name):
        return False

    def close_tab(self, name):
        return False

    def chat_widget(self):
        return self._cw


def test_open_dataset_no_note_method_fallback(ds_env, monkeypatch):
    """(c) note_current_dataset 無し window でフォールバック（cw.set_current_dataset）。"""
    import config
    monkeypatch.setattr(config, "reload_datasets", lambda config_path=None: None)
    session._touched.clear()

    class _FakeCW:
        def __init__(self):
            self.datasets = []
        def set_current_dataset(self, ds):
            self.datasets.append(ds)
        def merge_dataset_sessions(self, ds, sessions):
            pass

    cw = _FakeCW()
    win = _LegacyWindow()
    win._cw = cw
    result = session.open_dataset(win, "ds_a")
    assert result.startswith("no-session:")
    assert cw.datasets == ["ds_a"]


def test_open_dataset_reload_called(ds_env, monkeypatch):
    """(d) open_dataset は無条件で reload_datasets を呼ぶ。"""
    import config
    reload_calls = []

    def tracking_reload(config_path=None):
        reload_calls.append(1)

    monkeypatch.setattr(config, "reload_datasets", tracking_reload)
    session._touched.clear()
    win = _DispatchWindow()
    session.open_dataset(win, "ds_a")
    assert len(reload_calls) == 1


def test_open_dataset_nonexistent_dir_returns_error(ds_env, monkeypatch):
    """(e) dataset_dir が存在しない場合は error を返す。"""
    import config
    monkeypatch.setattr(config, "reload_datasets", lambda config_path=None: None)
    # get_dataset_dir が存在しないパスを返すようにする
    monkeypatch.setattr(
        config, "get_dataset_dir",
        lambda name: Path("/nonexistent/dataset/path"),
    )
    session._touched.clear()
    win = _DispatchWindow()
    result = session.open_dataset(win, "ds_a")
    assert result.startswith("error:")
    assert win.noted_datasets == []


# ---- active_tab focus dataset-match guard (reviewer P1 R2) ----

class _FocusWindow(_DispatchWindow):
    """Window whose add-tab simulates the same-name cross-dataset collision and
    whose tabs() already holds a foreign-dataset same-named tab."""

    def __init__(self, preexisting):
        super().__init__()
        self._tabs = list(preexisting)
        self.activated: list[str] = []

    def dispatch_command(self, verb, **kwargs):
        if verb == "add-tab":
            # §2a collision guard would raise for a same-name other-dataset tab.
            raise ValueError("same-named analysis already open for another dataset")

    def set_active_tab(self, name):
        self.activated.append(name)
        return True


def test_open_dataset_active_tab_focus_dataset_guard(ds_env):
    """dsA/demo が開いている状態で dsB を開くと、衝突で dsB/demo は skip され、
    かつ active_tab 復元が dsA/demo を誤 focus しない（reviewer P1 R2）。"""
    session._touched.clear()
    payload = {
        "version": 1, "dataset": "ds_b", "active_tab": "demo",
        "tabs": [{"name": "demo", "kind": "analysis", "module": "demo"}],
    }
    session.write_session("ds_b", payload)

    foreign = _FakeTab("demo", {"kind": "analysis", "name": "demo", "dataset": "ds_a"})
    win = _FocusWindow([foreign])
    result = session.open_dataset(win, "ds_b")
    assert result == "restored:0"  # collision → demo skipped
    assert win.activated == []  # foreign dsA/demo NOT focused
