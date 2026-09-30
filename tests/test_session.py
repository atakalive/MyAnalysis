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
    # helper の no-leftover: 書込後に固定名/一意名の tmp が残らない。
    work_dir = dataset_config.get_work_dir("ds_a")
    assert list(work_dir.glob("*.tmp")) == []


def test_read_missing_returns_none(ds_env):
    assert session.read_session("ds_a") is None


def test_read_bad_json_no_bak_raises(ds_env):
    """破損 JSON（.bak 無し）→ SessionUnreadableError。

    旧仕様の「破損 → None」は 0 バイト truncate（同期マウント障害）を
    no-session と混同し、タブ構成の黙殺消失になるため廃止。
    """
    work_dir = dataset_config.get_work_dir("ds_a")
    (work_dir / "session.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(session.SessionUnreadableError):
        session.read_session("ds_a")


def test_read_unknown_dataset_returns_none(ds_env):
    assert session.read_session("nope") is None


# ---- durable session.json（.bak 二重化 + read-back 検証）----

_PAYLOAD = {
    "version": 1,
    "dataset": "ds_a",
    "active_tab": "fig1",
    "tabs": [{"name": "fig1", "kind": "figure", "figure": "figures/fig1.png"}],
}


def test_write_session_writes_bak(ds_env):
    import json as _json

    from common.paths import strip_seq
    session.write_session("ds_a", _PAYLOAD)
    work_dir = dataset_config.get_work_dir("ds_a")
    bak = work_dir / "session.json.bak"
    # ディスク上には newest-wins 用の `_seq` が付く（Issue #96）。内容はそれ以外一致。
    assert strip_seq(_json.loads(bak.read_text(encoding="utf-8"))) == _PAYLOAD


def test_read_session_recovers_from_bak(ds_env):
    """primary が 0 バイト化しても .bak から復元される（今回の実障害の形）。"""
    session.write_session("ds_a", _PAYLOAD)
    work_dir = dataset_config.get_work_dir("ds_a")
    (work_dir / "session.json").write_text("", encoding="utf-8")
    assert session.read_session("ds_a") == _PAYLOAD


def test_write_session_truncated_write_raises(ds_env, monkeypatch):
    """書込が黙って空を書く（マウント truncate 相当）→ read-back 検証が raise。"""
    from common import paths as common_paths
    monkeypatch.setattr(
        common_paths, "atomic_write_text",
        lambda path, text, **k: Path(path).write_text("", encoding="utf-8"),
    )
    with pytest.raises(session.SessionPersistError):
        session.write_session("ds_a", _PAYLOAD)


def test_write_session_bak_truncate_detected(ds_env, monkeypatch):
    """.bak 側だけが truncate されても検出する（二重化の黙った劣化を許さない）。"""
    from common import paths as common_paths
    real = common_paths.atomic_write_text

    def selective(path, text, **k):
        if str(path).endswith(".bak"):
            Path(path).write_text("", encoding="utf-8")
        else:
            real(path, text, **k)

    monkeypatch.setattr(common_paths, "atomic_write_text", selective)
    with pytest.raises(session.SessionPersistError):
        session.write_session("ds_a", _PAYLOAD)


def test_read_session_absent_primary_corrupt_bak_raises(ds_env):
    """primary 消失 + .bak 破損 → no-session ではなく unreadable。

    no-session に丸めると次の保存が .bak を上書きし、マウント回復後なら読めた
    かもしれない最後の復旧材料を潰す（レビュー #92 指摘の残存損失経路）。
    """
    session.write_session("ds_a", _PAYLOAD)
    work_dir = dataset_config.get_work_dir("ds_a")
    (work_dir / "session.json").unlink()
    (work_dir / "session.json.bak").write_text("", encoding="utf-8")
    with pytest.raises(session.SessionUnreadableError):
        session.read_session("ds_a")


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


def test_save_all_truncated_write_marks_failed(ds_env, monkeypatch):
    """write_session の read-back 検証失敗が save_all の failed に載る。

    この failed 経路が Tier 3/4 リロードの中止（qt_integration の既存 abort）、
    「保存して終了」の close 拒否、close_dataset の中止を駆動する — つまり
    truncate をここで検出できれば黙殺消失は起きない。
    """
    session._touched.clear()
    spec_a = {"kind": "figure", "name": "fa", "dataset": "ds_a",
              "figure": str(ds_env["ds_a"] / "_work" / "fa.png")}
    win = _FakeWindow([_FakeTab("fa", spec_a)], active=None)
    from common import paths as common_paths
    monkeypatch.setattr(
        common_paths, "atomic_write_text",
        lambda path, text, **k: Path(path).write_text("", encoding="utf-8"),
    )
    saved, failed = session.save_all(win)
    assert saved == []
    assert failed == ["ds_a"]
    assert not win.dirty_cleared


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


def test_open_dataset_unreadable_session(ds_env):
    """(7d) 0 バイト session.json（.bak 無し）→ unreadable-session:。

    破損ファイルは上書きされず、_touched も汚染されず、DS 自体は開く
    （note_current_dataset が呼ばれる）。no-session と混同しない。
    """
    session._touched.clear()
    work_dir = dataset_config.get_work_dir("ds_a")
    (work_dir / "session.json").write_text("", encoding="utf-8")
    win = _DispatchWindow()
    result = session.open_dataset(win, "ds_a")
    assert result == "unreadable-session:ds_a"
    assert (work_dir / "session.json").read_text(encoding="utf-8") == ""
    assert "ds_a" not in session._touched
    assert win.noted_datasets == ["ds_a"]
    assert "ds_a" in session._unreadable
    assert "ds_a" not in session._restored


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


# ---- active_tab focus dataset-match guard ----

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
    かつ active_tab 復元が dsA/demo を誤 focus しない。"""
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


# ---- Issue #51: save_dataset / forget_dataset / last_window ride-along ----

def test_save_dataset_writes_one(ds_env):
    session._touched.clear()
    spec = {"kind": "figure", "name": "fa", "dataset": "ds_a",
            "figure": str(ds_env["ds_a"] / "_work" / "fa.png")}
    win = _FakeWindow([_FakeTab("fa", spec)], active=None)
    assert session.save_dataset(win, "ds_a") is True
    data = session.read_session("ds_a")
    assert data["tabs"] == [{"name": "fa", "kind": "figure", "figure": "fa.png"}]
    # per-dataset save must NOT touch the global dirty-clear gate.
    assert win.dirty_cleared is False


def test_save_dataset_zero_tabs_skips_write(ds_env):
    """0 tabs → True but nothing written (never persist an empty layout)."""
    win = _FakeWindow([], active=None)
    assert session.save_dataset(win, "ds_a") is True
    assert session.read_session("ds_a") is None


def test_save_dataset_failure_returns_false(ds_env, monkeypatch):
    spec = {"kind": "figure", "name": "fa", "dataset": "ds_a",
            "figure": str(ds_env["ds_a"] / "_work" / "fa.png")}
    win = _FakeWindow([_FakeTab("fa", spec)], active=None)
    monkeypatch.setattr(
        dataset_config, "get_work_dir",
        lambda name, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert session.save_dataset(win, "ds_a") is False


def test_forget_dataset():
    session._touched.clear()
    session.note_dataset("dsx")
    assert "dsx" in session._touched
    session.forget_dataset("dsx")
    assert "dsx" not in session._touched
    session.forget_dataset("dsx")  # idempotent, no raise


def test_save_all_writes_last_window(ds_env, monkeypatch, tmp_path):
    import json

    from llm_bridge import paths as lb_paths
    lw = tmp_path / "last_window.json"
    monkeypatch.setattr(lb_paths, "last_window_path", lambda: lw)
    session._touched.clear()

    class _WSWindow(_FakeWindow):
        current_dataset = "ds_a"

        def open_dataset_names(self):
            return ["ds_a", "ds_b"]

    spec = {"kind": "figure", "name": "fa", "dataset": "ds_a",
            "figure": str(ds_env["ds_a"] / "_work" / "fa.png")}
    win = _WSWindow([_FakeTab("fa", spec)], active=None)
    session.save_all(win)
    data = json.loads(lw.read_text(encoding="utf-8"))
    assert data == {"version": 1, "datasets": ["ds_a", "ds_b"], "active": "ds_a"}


def test_save_all_last_window_isolated_from_contract(ds_env, monkeypatch, tmp_path):
    """A last_window write failure must not affect (saved, failed) / dirty gate."""
    from llm_bridge import paths as lb_paths
    monkeypatch.setattr(
        lb_paths, "last_window_path",
        lambda: (_ for _ in ()).throw(RuntimeError("disk gone")),
    )
    session._touched.clear()
    spec = {"kind": "figure", "name": "fa", "dataset": "ds_a",
            "figure": str(ds_env["ds_a"] / "_work" / "fa.png")}
    win = _FakeWindow([_FakeTab("fa", spec)], active=None)
    saved, failed = session.save_all(win)
    assert saved == ["ds_a"]
    assert failed == []
    assert win.dirty_cleared  # dirty still cleared despite last_window failure


def test_read_last_window_missing_returns_empty(monkeypatch, tmp_path):
    from llm_bridge import paths as lb_paths
    monkeypatch.setattr(lb_paths, "last_window_path", lambda: tmp_path / "absent.json")
    assert session.read_last_window() == {}


# ---- Issue #71: split layout / figure2 persistence (Qt-free) ----

class _LayoutTab:
    """Stub tab exposing capture_layout + _panels for save-side layout/figure2."""
    def __init__(self, name, spec, layout=None, figure2_path=None):
        self.name = name
        self.session_spec = spec
        self._layout = layout
        self._panels = {}
        if figure2_path is not None:
            self._panels["figure-2"] = type("P", (), {"_path": figure2_path})()

    def capture_layout(self):
        return self._layout


class _RecordingDispatchWindow(_DispatchWindow):
    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, dict]] = []

    def dispatch_command(self, verb, **kwargs):
        self.calls.append((verb, dict(kwargs)))


def test_save_all_persists_layout_and_figure2(ds_env):
    session._touched.clear()
    wd = dataset_config.get_work_dir("ds_a")
    fig1 = wd / "figures" / "a.png"
    fig2 = wd / "figures" / "b.png"
    layout = {"orientation": "horizontal", "sizes": [620, 380],
              "left_hidden": False, "right_hidden": False}
    spec = {"kind": "figure", "name": "viewer", "dataset": "ds_a",
            "figure": str(fig1)}
    tab = _LayoutTab("viewer", spec, layout=layout, figure2_path=fig2)
    win = _FakeWindow([tab], active=None)
    session.save_all(win)
    data = session.read_session("ds_a")
    entry = data["tabs"][0]
    # session.json は cross-PC 資産なので相対パスは OS に依らず '/' 区切り
    # （as_posix。Windows ネイティブ区切りだと POSIX 復元でタブが黙って落ちる）。
    assert entry["figure"] == "figures/a.png"
    assert entry["figure2"] == "figures/b.png"
    assert entry["layout"] == layout


def test_save_dataset_persists_layout_and_figure2(ds_env):
    session._touched.clear()
    wd = dataset_config.get_work_dir("ds_a")
    fig1 = wd / "figures" / "a.png"
    fig2 = wd / "figures" / "b.png"
    layout = {"orientation": "vertical", "sizes": [300, 300],
              "left_hidden": False, "right_hidden": False}
    spec = {"kind": "figure", "name": "viewer", "dataset": "ds_a",
            "figure": str(fig1)}
    tab = _LayoutTab("viewer", spec, layout=layout, figure2_path=fig2)
    win = _FakeWindow([tab], active=None)
    assert session.save_dataset(win, "ds_a") is True
    entry = session.read_session("ds_a")["tabs"][0]
    assert entry["figure2"] == "figures/b.png"
    assert entry["layout"] == layout


def test_save_fake_tab_omits_layout_and_figure2(ds_env):
    """A _FakeTab (no capture_layout / _panels) yields an unchanged entry."""
    session._touched.clear()
    wd = dataset_config.get_work_dir("ds_a")
    spec = {"kind": "figure", "name": "fa", "dataset": "ds_a",
            "figure": str(wd / "fa.png")}
    win = _FakeWindow([_FakeTab("fa", spec)], active=None)
    session.save_all(win)
    entry = session.read_session("ds_a")["tabs"][0]
    assert "layout" not in entry
    assert "figure2" not in entry


def _write_bytes(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")


def test_open_dataset_figure2_second_show(ds_env):
    """(a) figure2 present + file exists → 2nd show with slot=right issued."""
    session._touched.clear()
    wd = dataset_config.get_work_dir("ds_a")
    _write_bytes(wd / "figures" / "a.png")
    _write_bytes(wd / "figures" / "b.png")
    session.write_session("ds_a", {
        "version": 1, "dataset": "ds_a", "active_tab": None,
        "tabs": [{
            "name": "viewer", "kind": "figure", "figure": "figures/a.png",
            "figure2": "figures/b.png",
            "layout": {"orientation": "horizontal", "sizes": [1, 1],
                       "left_hidden": False, "right_hidden": False},
        }],
    })
    win = _RecordingDispatchWindow()
    session.open_dataset(win, "ds_a")
    shows = [c for c in win.calls if c[0] == "show"]
    assert len(shows) == 2
    with_slot = [c for c in shows if "slot" in c[1]]
    assert len(with_slot) == 1
    assert with_slot[0][1]["slot"] == "right"


def test_open_dataset_figure2_missing_no_second_show(ds_env):
    """(b) figure2 present but file missing → no 2nd show, no slot kwarg."""
    session._touched.clear()
    wd = dataset_config.get_work_dir("ds_a")
    _write_bytes(wd / "figures" / "a.png")
    session.write_session("ds_a", {
        "version": 1, "dataset": "ds_a", "active_tab": None,
        "tabs": [{
            "name": "viewer", "kind": "figure", "figure": "figures/a.png",
            "figure2": "figures/gone.png",
            "layout": {"orientation": "horizontal", "sizes": [1, 1],
                       "left_hidden": False, "right_hidden": False},
        }],
    })
    win = _RecordingDispatchWindow()
    session.open_dataset(win, "ds_a")
    shows = [c for c in win.calls if c[0] == "show"]
    assert len(shows) == 1
    assert "slot" not in shows[0][1]


def test_open_dataset_image_panel_from_layout(ds_env):
    """(c) image entry with left_hidden=True → show-image panel='right'."""
    session._touched.clear()
    wd = dataset_config.get_work_dir("ds_a")
    _write_bytes(wd / "images" / "im.png")
    session.write_session("ds_a", {
        "version": 1, "dataset": "ds_a", "active_tab": None,
        "tabs": [{
            "name": "viewer", "kind": "image", "image": "images/im.png",
            "layout": {"orientation": "horizontal", "sizes": [0, 1],
                       "left_hidden": True, "right_hidden": False},
        }],
    })
    win = _RecordingDispatchWindow()
    session.open_dataset(win, "ds_a")
    imgs = [c for c in win.calls if c[0] == "show-image"]
    assert len(imgs) == 1
    assert imgs[0][1]["panel"] == "right"


def test_open_dataset_legacy_entries_no_layout(ds_env):
    """(d) old entries without layout/figure2 restore without error."""
    session._touched.clear()
    wd = dataset_config.get_work_dir("ds_a")
    _write_bytes(wd / "figures" / "a.png")
    _write_bytes(wd / "images" / "im.png")
    session.write_session("ds_a", {
        "version": 1, "dataset": "ds_a", "active_tab": None,
        "tabs": [
            {"name": "fig", "kind": "figure", "figure": "figures/a.png"},
            {"name": "img", "kind": "image", "image": "images/im.png"},
            {"name": "an", "kind": "analysis", "module": "an"},
        ],
    })
    win = _RecordingDispatchWindow()
    result = session.open_dataset(win, "ds_a")
    assert result == "restored:3"
    shows = [c for c in win.calls if c[0] == "show"]
    assert len(shows) == 1
    assert "slot" not in shows[0][1]
    imgs = [c for c in win.calls if c[0] == "show-image"]
    assert imgs[0][1]["panel"] == "left"
    adds = [c for c in win.calls if c[0] == "add-tab"]
    assert len(adds) == 1


# ---- Issue #97: nested panes (Qt-free) ----

def _panes_session(entry):
    session.write_session("ds_a", {
        "version": 1, "dataset": "ds_a", "active_tab": None, "tabs": [entry],
    })


def test_open_dataset_panes_dispatch_in_order(ds_env):
    session._touched.clear()
    wd = dataset_config.get_work_dir("ds_a")
    for n in ("a.png", "b.png", "c.tif"):
        _write_bytes(wd / n)
    _panes_session({
        "name": "q", "kind": "figure", "figure": "a.png",
        "panes": [
            {"slot": "top/left", "kind": "figure", "path": "a.png"},
            {"slot": "top/right", "kind": "figure", "path": "b.png"},
            {"slot": "bottom", "kind": "image", "path": "c.tif"},
        ],
    })
    win = _RecordingDispatchWindow()
    assert session.open_dataset(win, "ds_a") == "restored:1"
    calls = [(v, kw["slot"], Path(kw["path"]).name) for v, kw in win.calls]
    assert calls == [
        ("show", "top/left", "a.png"),
        ("show", "top/right", "b.png"),
        ("show-image", "bottom", "c.tif"),
    ]
    assert all(kw["dataset"] == "ds_a" and kw["name"] == "q" for _v, kw in win.calls)


def test_open_dataset_panes_invalid_excluded(ds_env):
    session._touched.clear()
    wd = dataset_config.get_work_dir("ds_a")
    _write_bytes(wd / "a.png")
    _write_bytes(wd / "b.png")
    _panes_session({
        "name": "q", "kind": "figure", "figure": "a.png",
        "panes": [
            {"slot": "left", "kind": "figure", "path": "a.png"},
            {"slot": "left/top", "kind": "figure", "path": "b.png"},    # conflict
            {"slot": "right/top", "kind": "figure", "path": "gone.png"},  # missing
            {"slot": None, "kind": "figure", "path": "b.png"},
            {"slot": "", "kind": "figure", "path": "b.png"},
            {"slot": "   ", "kind": "figure", "path": "b.png"},
            {"slot": 3, "kind": "figure", "path": "b.png"},
            {"slot": "right/top", "kind": "figure", "path": 5},
            {"slot": "right/top", "kind": "movie", "path": "b.png"},
            {"slot": "center", "kind": "figure", "path": "b.png"},
            "not-a-dict",
            {"slot": "right/bottom", "kind": "figure", "path": "b.png"},
        ],
    })
    win = _RecordingDispatchWindow()
    assert session.open_dataset(win, "ds_a") == "restored:1"
    assert [kw["slot"] for _v, kw in win.calls] == ["left", "right/bottom"]


def test_open_dataset_panes_all_fail_not_restored(ds_env):
    session._touched.clear()
    wd = dataset_config.get_work_dir("ds_a")
    _write_bytes(wd / "a.png")
    _panes_session({
        "name": "q", "kind": "figure", "figure": "a.png",
        "panes": [{"slot": "left", "kind": "figure", "path": "gone.png"}],
    })
    win = _RecordingDispatchWindow()
    assert session.open_dataset(win, "ds_a") == "restored:0"
    assert win.calls == []

    _panes_session({
        "name": "q", "kind": "figure", "figure": "a.png",
        "panes": [{"slot": "left", "kind": "figure", "path": "a.png"}],
    })

    class _Raising(_RecordingDispatchWindow):
        def dispatch_command(self, verb, **kwargs):
            super().dispatch_command(verb, **kwargs)
            raise RuntimeError("boom")

    win2 = _Raising()
    assert session.open_dataset(win2, "ds_a") == "restored:0"
    assert len(win2.calls) == 1


def test_legacy_expressible_table():
    f = session._legacy_expressible
    assert f([("left", "figure", "a")])
    assert f([("top", "figure", "a")])
    assert f([("left", "figure", "a"), ("right", "figure", "b")])
    assert f([("top", "figure", "a"), ("bottom", "figure", "b")])
    assert not f([("right", "figure", "a")])
    assert f([("left", "image", "a")])
    assert f([("right", "image", "a")])
    assert not f([("top", "image", "a")])
    assert not f([("bottom", "image", "a")])
    assert f([("top", "image", "a"), ("bottom", "image", "b")])
    assert not f([("left", "figure", "a"), ("right", "image", "b")])
    assert not f([("top/left", "figure", "a")])
    assert not f([])


def test_legacy_panes_table(ds_env):
    wd = dataset_config.get_work_dir("ds_a")
    _write_bytes(wd / "a.png")
    _write_bytes(wd / "b.png")
    lp = session._legacy_panes

    def rows(entry):
        out = lp(entry, wd)
        return None if out is None else [
            (r["slot"], r["kind"], Path(r["path"]).name, r["reuse_key"]) for r in out
        ]

    h = {"orientation": "horizontal"}
    v = {"orientation": "vertical"}
    assert rows({"kind": "figure", "figure": "a.png", "figure2": "b.png", "layout": h}) == [
        ("left", "figure", "a.png", "figure"), ("right", "figure", "b.png", "figure-2")]
    assert rows({"kind": "figure", "figure": "a.png", "figure2": "b.png", "layout": v}) == [
        ("top", "figure", "a.png", "figure"), ("bottom", "figure", "b.png", "figure-2")]
    assert rows({"kind": "figure", "figure": "a.png", "layout": h}) == [
        ("left", "figure", "a.png", "figure")]
    assert rows({"kind": "figure", "figure": "a.png", "figure2": "gone.png"}) == [
        ("left", "figure", "a.png", "figure")]
    assert rows({"kind": "image", "image": "a.png", "image2": "b.png", "layout": h}) == [
        ("left", "image", "a.png", "viewer"), ("right", "image", "b.png", "viewer-2")]
    assert rows({"kind": "image", "image": "a.png",
                 "layout": {**h, "left_hidden": True}}) == [
        ("right", "image", "a.png", "viewer")]
    assert rows({"kind": "image", "image": "a.png", "layout": h}) == [
        ("left", "image", "a.png", "viewer")]
    # image2 field present but file missing + left_hidden → single on the right.
    assert rows({"kind": "image", "image": "a.png", "image2": "gone.png",
                 "layout": {**h, "left_hidden": True}}) == [
        ("right", "image", "a.png", "viewer")]
    assert rows({"kind": "image", "image": "gone.png", "layout": h}) is None
    assert rows({"kind": "figure", "figure": "gone.png"}) is None


class _HostTab:
    """Fake viewer host tab recording the restore hooks."""

    def __init__(self, name, dataset, kind="figure"):
        self.name = name
        self.viewer_host = True
        self.session_spec = {"kind": kind, "name": name, "dataset": dataset}
        self.log: list = []

    def prune_panes(self, keep):
        self.log.append(("prune", set(keep)))

    def apply_layout(self, layout):
        self.log.append(("apply_layout", dict(layout)))

    def tidy(self):
        self.log.append(("tidy",))


def _place_recorder(monkeypatch):
    import llm_bridge
    calls = []

    def rec(tab, slot, kind, abs_path, *, reuse_key=None, claimed=frozenset()):
        calls.append({"slot": slot, "kind": kind, "path": Path(abs_path).name,
                      "reuse_key": reuse_key, "claimed": set(claimed)})
        return f"K{len(calls)}", object()

    monkeypatch.setattr(llm_bridge, "_place_viewer", rec)
    return calls


def test_route_legacy_existing_host_uses_place_viewer(ds_env, monkeypatch):
    session._touched.clear()
    wd = dataset_config.get_work_dir("ds_a")
    _write_bytes(wd / "a.png")
    _write_bytes(wd / "b.png")
    calls = _place_recorder(monkeypatch)
    _panes_session({
        "name": "v", "kind": "image", "image": "a.png", "image2": "b.png",
        "layout": {"orientation": "vertical", "sizes": [1, 1],
                   "left_hidden": False, "right_hidden": False},
    })
    win = _RecordingDispatchWindow()
    t0 = _HostTab("v", "ds_a", kind="image")
    win._tabs = [t0]
    assert session.open_dataset(win, "ds_a") == "restored:1"
    assert win.calls == []
    assert [(c["slot"], c["path"], c["reuse_key"]) for c in calls] == [
        ("top", "a.png", "viewer"), ("bottom", "b.png", "viewer-2")]
    assert calls[0]["claimed"] == set()
    assert calls[1]["claimed"] == {"K1"}
    assert t0.log[0] == ("prune", {"top", "bottom"})
    applied = t0.log[1][1]
    assert "left_hidden" not in applied and "right_hidden" not in applied
    assert applied["orientation"] == "vertical"
    assert t0.log[2] == ("tidy",)


def test_route_legacy_minimal_existing_host_plain_dispatch(ds_env, monkeypatch):
    session._touched.clear()
    wd = dataset_config.get_work_dir("ds_a")
    _write_bytes(wd / "a.png")
    calls = _place_recorder(monkeypatch)
    _panes_session({"name": "v", "kind": "figure", "figure": "a.png"})
    win = _RecordingDispatchWindow()
    t0 = _HostTab("v", "ds_a")
    win._tabs = [t0]
    assert session.open_dataset(win, "ds_a") == "restored:1"
    assert len(win.calls) == 1 and "slot" not in win.calls[0][1]
    assert calls == []
    assert t0.log == []


def test_route_legacy_new_tab_plain_dispatch(ds_env, monkeypatch):
    session._touched.clear()
    wd = dataset_config.get_work_dir("ds_a")
    _write_bytes(wd / "a.png")
    _write_bytes(wd / "b.png")
    calls = _place_recorder(monkeypatch)
    _panes_session({
        "name": "v", "kind": "figure", "figure": "a.png", "figure2": "b.png",
        "layout": {"orientation": "horizontal", "sizes": [1, 1],
                   "left_hidden": False, "right_hidden": False},
    })
    win = _RecordingDispatchWindow()
    assert session.open_dataset(win, "ds_a") == "restored:1"
    assert [kw.get("slot") for _v, kw in win.calls] == [None, "right"]
    assert calls == []


def test_route_panes_existing_host_ignores_reuse_key(ds_env, monkeypatch):
    session._touched.clear()
    wd = dataset_config.get_work_dir("ds_a")
    _write_bytes(wd / "a.png")
    calls = _place_recorder(monkeypatch)
    _panes_session({
        "name": "v", "kind": "figure", "figure": "a.png",
        "panes": [{"slot": "top/left", "kind": "figure", "path": "a.png",
                   "reuse_key": "figure"}],
    })
    win = _RecordingDispatchWindow()
    t0 = _HostTab("v", "ds_a")
    win._tabs = [t0]
    assert session.open_dataset(win, "ds_a") == "restored:1"
    assert calls[0]["slot"] == "top/left"
    assert calls[0]["reuse_key"] is None
    assert t0.log[0] == ("prune", {"top/left"})


def test_route_existing_analysis_tab_not_clobbered(ds_env, monkeypatch):
    session._touched.clear()
    wd = dataset_config.get_work_dir("ds_a")
    _write_bytes(wd / "a.png")
    calls = _place_recorder(monkeypatch)
    _panes_session({
        "name": "v", "kind": "figure", "figure": "a.png",
        "panes": [{"slot": "left", "kind": "figure", "path": "a.png"}],
    })
    win = _RecordingDispatchWindow()
    t0 = _FakeTab("v", {"kind": "analysis", "name": "v", "dataset": "ds_a"})
    win._tabs = [t0]
    assert session.open_dataset(win, "ds_a") == "restored:0"
    assert calls == [] and win.calls == []


def test_tree_to_entry_panes_format(tmp_path):
    class _T:
        def pane_contents(self):
            return [("top/left", "figure", str(tmp_path / "a.png")),
                    ("bottom", "image", str(tmp_path / "b.tif"))]

        def capture_layout(self):
            return {"orientation": "vertical", "sizes": [1, 1],
                    "left_hidden": True, "right_hidden": False,
                    "splits": {"top": [1, 1]}}

    spec = {"kind": "figure", "name": "q", "dataset": "d", "figure": "x"}
    entry = session._spec_to_tab(spec, tmp_path, _T())
    assert entry["kind"] == "figure" and entry["figure"] == "a.png"
    assert entry["panes"] == [
        {"slot": "top/left", "kind": "figure", "path": "a.png"},
        {"slot": "bottom", "kind": "image", "path": "b.tif"},
    ]
    assert entry["layout"]["left_hidden"] is False
    assert entry["layout"]["splits"] == {"top": [1, 1]}


def test_tree_to_entry_empty_not_saved(tmp_path):
    class _T:
        def pane_contents(self):
            return []

        def capture_layout(self):
            return {}

    spec = {"kind": "image", "name": "q", "dataset": "d", "image": "x"}
    assert session._spec_to_tab(spec, tmp_path, _T()) is None


# ==== Issue #106: 保存経路でデータを消さない ====

import copy  # noqa: E402

from common import paths as common_paths  # noqa: E402
from llm_backend.base import Message  # noqa: E402
from llm_bridge import chat_store  # noqa: E402


def _fig_tab(ds_env, ds="ds_a", name="fa"):
    spec = {"kind": "figure", "name": name, "dataset": ds,
            "figure": str(ds_env[ds] / "_work" / f"{name}.png")}
    return _FakeTab(name, spec)


def _payload(ds="ds_a", names=("old",)):
    return {
        "version": 1, "dataset": ds, "active_tab": None,
        "tabs": [{"name": n, "kind": "figure", "figure": f"figures/{n}.png"}
                 for n in names],
    }


def _snap(work_dir):
    target = work_dir / "session.json"
    return tuple(
        p.read_bytes() if p.exists() else None
        for p in (target, common_paths.bak_path(target))
    )


# ---- A-1 ----

def test_save_all_skips_existing_session_of_unopened_dataset(ds_env):
    session._touched.clear()
    session.write_session("ds_a", _payload())
    work_dir = dataset_config.get_work_dir("ds_a")
    before = _snap(work_dir)
    assert None not in before
    win = _FakeWindow([_fig_tab(ds_env)])
    saved, failed = session.save_all(win)
    assert _snap(work_dir) == before
    assert saved == []
    assert failed == []
    assert session.skipped_datasets() == {"ds_a": "not-opened"}
    assert win.dirty_cleared is True


def test_save_all_skips_when_only_bak_exists(ds_env):
    session._touched.clear()
    session.write_session("ds_a", _payload())
    work_dir = dataset_config.get_work_dir("ds_a")
    (work_dir / "session.json").unlink()
    before = _snap(work_dir)
    session.save_all(_FakeWindow([_fig_tab(ds_env)]))
    assert _snap(work_dir) == before
    assert not (work_dir / "session.json").exists()
    assert session.skipped_datasets() == {"ds_a": "not-opened"}


def test_save_all_new_dataset_written_and_stays_writable(ds_env):
    session._touched.clear()
    saved, _ = session.save_all(_FakeWindow([_fig_tab(ds_env)]))
    assert saved == ["ds_a"]
    win2 = _FakeWindow([_fig_tab(ds_env), _fig_tab(ds_env, name="fb")])
    saved, failed = session.save_all(win2)
    assert saved == ["ds_a"] and failed == []
    assert len(session.read_session("ds_a")["tabs"]) == 2


def test_save_all_writes_after_open_dataset(ds_env):
    session._touched.clear()
    session.write_session("ds_a", _payload())
    session.open_dataset(_DispatchWindow(), "ds_a")
    saved, failed = session.save_all(_FakeWindow([_fig_tab(ds_env)]))
    assert saved == ["ds_a"] and failed == []
    assert [t["name"] for t in session.read_session("ds_a")["tabs"]] == ["fa"]


def _move_ds_a(ds_env, tmp_path):
    new_dir = tmp_path / "ds_a_moved"
    new_dir.mkdir()
    ds_env["ds_a"] = new_dir
    return dataset_config.get_work_dir("ds_a")


def test_save_all_skips_when_work_dir_changed_after_open(ds_env, tmp_path):
    session._touched.clear()
    session.write_session("ds_a", _payload())
    session.open_dataset(_DispatchWindow(), "ds_a")
    wd_b = _move_ds_a(ds_env, tmp_path)
    session.write_session("ds_a", _payload(names=("other",)))
    before = _snap(wd_b)
    saved, failed = session.save_all(_FakeWindow([_fig_tab(ds_env)]))
    assert _snap(wd_b) == before
    assert saved == [] and failed == []
    assert session.skipped_datasets() == {"ds_a": "work-dir-changed"}


def test_save_all_writes_when_work_dir_changed_to_empty(ds_env, tmp_path):
    session._touched.clear()
    session.write_session("ds_a", _payload())
    session.open_dataset(_DispatchWindow(), "ds_a")
    wd_b = _move_ds_a(ds_env, tmp_path)
    saved, failed = session.save_all(_FakeWindow([_fig_tab(ds_env)]))
    assert saved == ["ds_a"] and failed == []
    assert (wd_b / "session.json").exists()
    assert session._restored["ds_a"] == session._norm_dir(wd_b)


def _split_work_dir(monkeypatch, tmp_path):
    """get_work_dir: 1 回目は A（session.json 無し）、2 回目以降は B（session.json 有り）。"""
    wd_a = tmp_path / "wd_A"
    wd_b = tmp_path / "wd_B"
    wd_a.mkdir()
    wd_b.mkdir()
    session._write_session_at(wd_b, "ds_a", _payload(names=("b",)))
    calls = []

    def fake(name, create=True):
        calls.append(name)
        return wd_a if len(calls) == 1 else wd_b

    monkeypatch.setattr(dataset_config, "get_work_dir", fake)
    return wd_a, wd_b


def test_save_all_writes_to_single_resolved_work_dir(ds_env, monkeypatch, tmp_path):
    session._touched.clear()
    wd_a, wd_b = _split_work_dir(monkeypatch, tmp_path)
    before = _snap(wd_b)
    saved, failed = session.save_all(_FakeWindow([_fig_tab(ds_env)]))
    assert saved == ["ds_a"] and failed == []
    assert (wd_a / "session.json").exists()
    assert _snap(wd_b) == before


def test_save_dataset_writes_to_single_resolved_work_dir(ds_env, monkeypatch, tmp_path):
    session._touched.clear()
    wd_a, wd_b = _split_work_dir(monkeypatch, tmp_path)
    before = _snap(wd_b)
    assert session.save_dataset(_FakeWindow([_fig_tab(ds_env)]), "ds_a") is True
    assert (wd_a / "session.json").exists()
    assert _snap(wd_b) == before


def test_open_dataset_resolves_work_dir_once(ds_env, monkeypatch):
    session._touched.clear()
    session.write_session("ds_a", _payload(names=()))
    work_dir = dataset_config.get_work_dir("ds_a")
    orig = session._resolve_work_dir_readonly
    calls = []

    def once(name):
        calls.append(name)
        if len(calls) == 1:
            return orig(name)
        raise RuntimeError("transient")

    monkeypatch.setattr(session, "_resolve_work_dir_readonly", once)
    result = session.open_dataset(_DispatchWindow(), "ds_a")
    assert not result.startswith("no-session:")
    assert session._restored["ds_a"] == session._norm_dir(work_dir)


def test_save_dataset_skips_existing_session_of_unopened_dataset(ds_env):
    session._touched.clear()
    session.write_session("ds_a", _payload())
    work_dir = dataset_config.get_work_dir("ds_a")
    before = _snap(work_dir)
    assert session.save_dataset(_FakeWindow([_fig_tab(ds_env)]), "ds_a") is True
    assert _snap(work_dir) == before


def test_forget_dataset_clears_restored(ds_env):
    session._touched.clear()
    session.open_dataset(_DispatchWindow(), "ds_a")
    assert "ds_a" in session._restored
    session.forget_dataset("ds_a")
    assert "ds_a" not in session._restored


# ---- A-4 ----

def _open_unreadable(ds="ds_a"):
    work_dir = dataset_config.get_work_dir(ds)
    (work_dir / "session.json").write_text("", encoding="utf-8")
    assert session.open_dataset(_DispatchWindow(), ds) == f"unreadable-session:{ds}"
    return work_dir


def test_unreadable_session_not_overwritten_until_cleared(ds_env):
    session._touched.clear()
    work_dir = _open_unreadable()
    win = _FakeWindow([_fig_tab(ds_env)])
    saved, failed = session.save_all(win)
    assert (work_dir / "session.json").read_text(encoding="utf-8") == ""
    assert session.skipped_datasets() == {"ds_a": "unreadable"}
    assert failed == [] and saved == []
    session.clear_unreadable("ds_a")
    saved, failed = session.save_all(win)
    assert saved == ["ds_a"] and failed == []
    assert len(session.read_session("ds_a")["tabs"]) == 1


def test_unreadable_session_not_overwritten_by_save_dataset(ds_env):
    session._touched.clear()
    work_dir = _open_unreadable()
    assert session.save_dataset(_FakeWindow([_fig_tab(ds_env)]), "ds_a") is True
    assert (work_dir / "session.json").read_text(encoding="utf-8") == ""


def test_unreadable_reopen_after_repair_clears_flag(ds_env):
    session._touched.clear()
    _open_unreadable()
    session.write_session("ds_a", _payload(names=()))
    session.open_dataset(_DispatchWindow(), "ds_a")
    assert "ds_a" in session._restored
    assert "ds_a" not in session._unreadable


def test_unreadable_save_targets(ds_env):
    session._touched.clear()
    _open_unreadable()
    assert session.unreadable_save_targets(_FakeWindow([_fig_tab(ds_env)])) == ["ds_a"]
    assert session.unreadable_save_targets(_FakeWindow([])) == []


# ---- A-2 / A-3: chat persistence ----

def _chat(ds, text="hi", title="t"):
    s = chat_store.new_session("mock", "sys", dataset=ds, title=title)
    s.messages.append(Message(role="user", content=text))
    return s


class _ChatFakeWindow(_FakeWindow):
    def __init__(self, sessions=(), tabs=None, deleted=()):
        super().__init__(list(tabs or []), active=None)
        self._sessions = list(sessions)
        self._deleted = set(deleted)
        self.refuse_merge = False
        self.merge_calls = 0

    def chat_sessions(self):
        return list(self._sessions)

    def chat_deleted_sessions(self):
        return set(self._deleted)

    def chat_clear_deleted(self, applied):
        self._deleted -= set(applied)

    def chat_merge_sessions(self, ds, sessions):
        self.merge_calls += 1
        if self.refuse_merge:
            return set()
        by_id = {s.id: s for s in sessions}
        out = set()
        for i, cur in enumerate(self._sessions):
            if cur.id in by_id:
                self._sessions[i] = by_id[cur.id]
                out.add(cur.id)
        return out


def _chat_path(work_dir, sid):
    return work_dir / "chat_sessions" / f"{sid}.json"


def _chat_bytes(work_dir, sid):
    p = _chat_path(work_dir, sid)
    return tuple(
        q.read_bytes() if q.exists() else None
        for q in (p, common_paths.bak_path(p))
    )


def _disk_copy(sess, *, text=None, updated=None, order=None):
    d = copy.deepcopy(sess)
    if text is not None:
        d.messages[-1] = Message(role="user", content=text)
    if updated is not None:
        d.updated = updated
    if order is not None:
        d.order = order
    return d


def _set_base(ds, sess, work_dir, order=0.0):
    """sess の今の内容を baseline（最後にディスクと一致した版）として置く。"""
    session._chat_baseline[(ds, sess.id)] = session.ChatBaseline(
        session._norm_dir(work_dir), chat_store.content_fingerprint(sess), order
    )


def _fp(sess):
    return chat_store.content_fingerprint(sess)


def _fail_chat_writes(monkeypatch, sids):
    orig = common_paths.atomic_write_text

    def fake(path, text, **k):
        if any(Path(path).name.startswith(f"{sid}.json") for sid in sids):
            raise OSError("mount write failed")
        return orig(path, text, **k)

    monkeypatch.setattr(common_paths, "atomic_write_text", fake)


def test_save_all_chat_write_failure_marks_failed(ds_env, monkeypatch):
    session._touched.clear()
    s = _chat("ds_a")
    _fail_chat_writes(monkeypatch, [s.id])
    win = _ChatFakeWindow([s])
    saved, failed = session.save_all(win)
    assert "ds_a" in failed
    assert win.dirty_cleared is False


def test_save_all_chat_failure_other_sessions_still_written(ds_env, monkeypatch):
    session._touched.clear()
    bad, good = _chat("ds_a"), _chat("ds_a")
    _fail_chat_writes(monkeypatch, [bad.id])
    saved, failed = session.save_all(_ChatFakeWindow([bad, good]))
    work_dir = dataset_config.get_work_dir("ds_a")
    assert "ds_a" in failed
    assert _chat_path(work_dir, good.id).exists()


def test_save_all_chat_unregistered_dataset_skipped(ds_env):
    session._touched.clear()
    win = _ChatFakeWindow([_chat("ds_gone")])
    saved, failed = session.save_all(win)
    assert failed == []
    assert win.dirty_cleared is True


def test_save_all_chat_delete_failure_marks_failed(ds_env, monkeypatch):
    session._touched.clear()
    monkeypatch.setattr(chat_store, "delete_session_file", lambda wd, sid: False)
    win = _ChatFakeWindow([], deleted={("ds_a", "x")})
    saved, failed = session.save_all(win)
    assert "ds_a" in failed
    assert ("ds_a", "x") in win._deleted


def test_save_all_chat_failure_not_duplicated_in_failed(ds_env, monkeypatch):
    session._touched.clear()
    monkeypatch.setattr(
        common_paths, "atomic_write_text",
        lambda *a, **k: (_ for _ in ()).throw(OSError("all writes fail")),
    )
    win = _ChatFakeWindow([_chat("ds_a")], tabs=[_fig_tab(ds_env)])
    saved, failed = session.save_all(win)
    assert failed.count("ds_a") == 1


def _disk_changed_setup(text="disk", delta=50.0):
    work_dir = dataset_config.get_work_dir("ds_a")
    mem = _chat("ds_a", "mem")
    disk = _disk_copy(mem, text=text, updated=mem.updated + delta, order=0.0)
    chat_store.write_session_file(work_dir, disk)
    _set_base("ds_a", mem, work_dir, 0.0)
    return work_dir, mem, disk


def test_chat_disk_only_change_is_adopted_not_written(ds_env):
    session._touched.clear()
    work_dir, mem, disk = _disk_changed_setup()
    before = _chat_bytes(work_dir, mem.id)
    win = _ChatFakeWindow([mem])
    saved, failed = session.save_all(win)
    assert _chat_bytes(work_dir, mem.id) == before
    assert win._sessions[0].messages[-1].content == "disk"
    assert session._chat_baseline[("ds_a", mem.id)].fingerprint == _fp(disk)
    assert win.merge_calls == 1
    assert failed == []


def test_chat_disk_change_adopted_even_if_older_updated(ds_env):
    session._touched.clear()
    work_dir, mem, disk = _disk_changed_setup(delta=-50.0)
    before = _chat_bytes(work_dir, mem.id)
    win = _ChatFakeWindow([mem])
    session.save_all(win)
    assert _chat_bytes(work_dir, mem.id) == before
    assert win._sessions[0].messages[-1].content == "disk"


def test_chat_adopt_batched_per_dataset(ds_env):
    session._touched.clear()
    work_dir = dataset_config.get_work_dir("ds_a")
    mems = [_chat("ds_a", "m1"), _chat("ds_a", "m2")]
    for i, m in enumerate(mems):
        chat_store.write_session_file(
            work_dir, _disk_copy(m, text="d", updated=m.updated + 5, order=float(i)))
        _set_base("ds_a", m, work_dir, float(i))
    win = _ChatFakeWindow(mems)
    session.save_all(win)
    assert win.merge_calls == 1
    assert [s.messages[-1].content for s in win._sessions] == ["d", "d"]


def _written_setup(text="hi"):
    """ディスク＝メモリ＝baseline の状態を作る。"""
    work_dir = dataset_config.get_work_dir("ds_a")
    mem = _chat("ds_a", text)
    mem.order = 0.0
    chat_store.write_session_file(work_dir, _disk_copy(mem))
    _set_base("ds_a", mem, work_dir, 0.0)
    return work_dir, mem


def _read_disk(work_dir, sid):
    status, sess = chat_store.read_session_file_status(work_dir, sid)
    assert status == "ok"
    return sess


def test_chat_local_only_change_written(ds_env):
    session._touched.clear()
    work_dir, mem = _written_setup()
    mem.messages.append(Message(role="assistant", content="local"))
    mem.updated += 10
    session.save_all(_ChatFakeWindow([mem]))
    disk = _read_disk(work_dir, mem.id)
    assert disk.messages[-1].content == "local"
    assert session._chat_baseline[("ds_a", mem.id)].fingerprint == _fp(mem)


def test_chat_both_changed_local_wins(ds_env):
    session._touched.clear()
    work_dir, mem = _written_setup()
    chat_store.write_session_file(
        work_dir, _disk_copy(mem, text="disk", updated=mem.updated + 20))
    mem.messages[-1] = Message(role="user", content="mem")
    mem.updated += 10
    session.save_all(_ChatFakeWindow([mem]))
    assert _read_disk(work_dir, mem.id).messages[-1].content == "mem"


def test_chat_local_edit_with_smaller_updated_still_written(ds_env):
    session._touched.clear()
    work_dir, mem = _written_setup()
    mem.messages[-1] = Message(role="user", content="mem")
    mem.updated -= 50
    session.save_all(_ChatFakeWindow([mem]))
    assert _read_disk(work_dir, mem.id).messages[-1].content == "mem"


def test_chat_unchanged_session_not_rewritten(ds_env):
    session._touched.clear()
    work_dir, mem = _written_setup()
    before = _chat_bytes(work_dir, mem.id)
    win = _ChatFakeWindow([mem])
    saved, failed = session.save_all(win)
    assert _chat_bytes(work_dir, mem.id) == before
    assert win.merge_calls == 0 and failed == []


def test_chat_reorder_elsewhere_not_clobbered(ds_env):
    session._touched.clear()
    work_dir, mem = _written_setup()
    chat_store.write_session_file(work_dir, _disk_copy(mem, order=3.0))
    before = _chat_bytes(work_dir, mem.id)
    session.save_all(_ChatFakeWindow([mem]))
    assert _chat_bytes(work_dir, mem.id) == before
    assert _read_disk(work_dir, mem.id).order == 3.0


def _two_written():
    work_dir = dataset_config.get_work_dir("ds_a")
    a, b = _chat("ds_a", "a"), _chat("ds_a", "b")
    for i, s in enumerate((a, b)):
        s.order = float(i)
        chat_store.write_session_file(work_dir, _disk_copy(s))
        _set_base("ds_a", s, work_dir, float(i))
    return work_dir, a, b


def test_chat_local_reorder_written(ds_env):
    session._touched.clear()
    work_dir, a, b = _two_written()
    session.save_all(_ChatFakeWindow([b, a]))
    assert _read_disk(work_dir, b.id).order == 0.0
    assert _read_disk(work_dir, a.id).order == 1.0


def test_chat_local_reorder_loses_to_disk_content_change(ds_env):
    session._touched.clear()
    work_dir, a, b = _two_written()
    chat_store.write_session_file(
        work_dir, _disk_copy(a, text="disk", updated=a.updated + 5, order=0.0))
    before = _chat_bytes(work_dir, a.id)
    win = _ChatFakeWindow([b, a])
    session.save_all(win)
    assert _chat_bytes(work_dir, a.id) == before
    assert win._sessions[1].messages[-1].content == "disk"


def test_chat_same_updated_other_body_not_clobbered_by_reorder(ds_env):
    """時計が baseline より遅れた 2 台は、どちらも max(time, T + 1e-3) で同じ updated を作る。
    他の PC が後から同じ updated・別の本文で保存した版を、手元の並べ替えの保存で
    書き戻さない（内容の同一性は updated でなく指紋で見る）。"""
    session._touched.clear()
    work_dir, a, b = _two_written()
    t1 = a.updated + 1e-3                       # 両 PC が作る同じ updated
    a.messages[-1] = Message(role="user", content="mine")
    a.updated = t1
    session.save_all(_ChatFakeWindow([a, b]))   # 手元の版を保存（baseline＝手元の版）
    assert _read_disk(work_dir, a.id).messages[-1].content == "mine"
    theirs = _disk_copy(a, text="theirs", updated=t1, order=0.0)
    chat_store.write_session_file(work_dir, theirs)   # 他の PC が後から保存
    before = _chat_bytes(work_dir, a.id)
    win = _ChatFakeWindow([b, a])               # 手元で並べ替え
    saved, failed = session.save_all(win)
    assert _chat_bytes(work_dir, a.id) == before
    assert _read_disk(work_dir, a.id).messages[-1].content == "theirs"
    assert win._sessions[1].messages[-1].content == "theirs"   # 取り込まれる
    assert failed == []


def test_chat_content_change_without_updated_change_written(ds_env):
    session._touched.clear()
    work_dir, mem = _written_setup()
    mem.messages[-1] = Message(role="user", content="edited")   # updated は変えない
    session.save_all(_ChatFakeWindow([mem]))
    assert _read_disk(work_dir, mem.id).messages[-1].content == "edited"


def test_chat_deleted_elsewhere_not_resurrected(ds_env):
    session._touched.clear()
    work_dir = dataset_config.get_work_dir("ds_a")
    mem = _chat("ds_a")
    _set_base("ds_a", mem, work_dir, 0.0)
    saved, failed = session.save_all(_ChatFakeWindow([mem]))
    assert not _chat_path(work_dir, mem.id).exists()
    assert failed == []
    # 位置だけが変わった版でも作られない。
    a, b = _chat("ds_a"), _chat("ds_a")
    _set_base("ds_a", a, work_dir, 0.0)
    _set_base("ds_a", b, work_dir, 1.0)
    saved, failed = session.save_all(_ChatFakeWindow([b, a]))
    assert not _chat_path(work_dir, a.id).exists()
    assert not _chat_path(work_dir, b.id).exists()
    assert failed == []


def test_chat_deleted_elsewhere_local_edit_rewritten(ds_env):
    session._touched.clear()
    work_dir = dataset_config.get_work_dir("ds_a")
    mem = _chat("ds_a")
    _set_base("ds_a", mem, work_dir, 0.0)
    mem.updated += 1
    session.save_all(_ChatFakeWindow([mem]))
    assert _chat_path(work_dir, mem.id).exists()


def test_chat_new_session_without_baseline_written(ds_env):
    session._touched.clear()
    work_dir = dataset_config.get_work_dir("ds_a")
    mem = _chat("ds_a")
    session.save_all(_ChatFakeWindow([mem]))
    assert _chat_path(work_dir, mem.id).exists()
    assert session._chat_baseline[("ds_a", mem.id)].fingerprint == _fp(mem)


def test_chat_work_dir_changed_absent_written(ds_env, tmp_path):
    session._touched.clear()
    work_dir = dataset_config.get_work_dir("ds_a")
    old = tmp_path / "old_wd"
    old.mkdir()
    mem = _chat("ds_a")
    _set_base("ds_a", mem, old, 0.0)
    session.save_all(_ChatFakeWindow([mem]))
    assert _chat_path(work_dir, mem.id).exists()
    assert session._chat_baseline[("ds_a", mem.id)].work_dir == session._norm_dir(work_dir)


def _moved_same_id_setup(tmp_path):
    wd_b = dataset_config.get_work_dir("ds_a")
    wd_a = tmp_path / "old_wd"
    wd_a.mkdir()
    mem = _chat("ds_a", "A")
    mem.order = 0.0
    chat_store.write_session_file(wd_a, _disk_copy(mem))
    chat_store.write_session_file(
        wd_b, _disk_copy(mem, text="B", updated=mem.updated + 3))
    _set_base("ds_a", mem, wd_a, 0.0)
    return wd_b, mem


def test_chat_work_dir_changed_same_id_unchanged_adopted(ds_env, tmp_path):
    session._touched.clear()
    wd_b, mem = _moved_same_id_setup(tmp_path)
    before = _chat_bytes(wd_b, mem.id)
    win = _ChatFakeWindow([mem])
    saved, failed = session.save_all(win)
    assert _chat_bytes(wd_b, mem.id) == before
    assert win._sessions[0].messages[-1].content == "B"
    assert failed == []


def test_chat_work_dir_changed_same_id_local_edit_fails(ds_env, tmp_path):
    session._touched.clear()
    wd_b, mem = _moved_same_id_setup(tmp_path)
    mem.updated += 1
    before = _chat_bytes(wd_b, mem.id)
    saved, failed = session.save_all(_ChatFakeWindow([mem]))
    assert _chat_bytes(wd_b, mem.id) == before
    assert "ds_a" in failed


def test_chat_adopt_refused_keeps_baseline(ds_env):
    session._touched.clear()
    work_dir, mem, disk = _disk_changed_setup()
    base = session._chat_baseline[("ds_a", mem.id)]
    before = _chat_bytes(work_dir, mem.id)
    win = _ChatFakeWindow([mem])
    win.refuse_merge = True
    session.save_all(win)
    assert _chat_bytes(work_dir, mem.id) == before
    assert session._chat_baseline[("ds_a", mem.id)] == base


def _unreadable_chat(monkeypatch, sid):
    orig = common_paths.read_json_classified

    def fake(path, *a, **k):
        if Path(path).name in (f"{sid}.json", f"{sid}.json.bak"):
            return ("unreadable", None)
        return orig(path, *a, **k)

    monkeypatch.setattr(common_paths, "read_json_classified", fake)


def test_chat_unreadable_disk_unchanged_local_fails_without_write(ds_env, monkeypatch):
    session._touched.clear()
    work_dir, mem = _written_setup()
    before = _chat_bytes(work_dir, mem.id)
    _unreadable_chat(monkeypatch, mem.id)
    win = _ChatFakeWindow([mem])
    saved, failed = session.save_all(win)
    assert _chat_bytes(work_dir, mem.id) == before
    assert "ds_a" in failed
    assert win.dirty_cleared is False


def test_chat_unreadable_disk_local_change_written(ds_env, monkeypatch):
    session._touched.clear()
    work_dir, mem = _written_setup()
    before = _chat_bytes(work_dir, mem.id)
    _unreadable_chat(monkeypatch, mem.id)
    mem.messages[-1] = Message(role="user", content="local")
    mem.updated += 1
    session.save_all(_ChatFakeWindow([mem]))
    assert _chat_bytes(work_dir, mem.id) != before
    assert "local" in _chat_path(work_dir, mem.id).read_text(encoding="utf-8")


def test_save_chat_sessions_dataset_order_deterministic(ds_env, monkeypatch):
    session._touched.clear()
    sb, sa = _chat("ds_b"), _chat("ds_a")
    _fail_chat_writes(monkeypatch, [sb.id, sa.id])
    assert session._save_chat_sessions(_ChatFakeWindow([sb, sa])) == ["ds_b", "ds_a"]


# ---- A-3: open_dataset records the chat baseline ----

class _MergingCW:
    """chat_store.merge_sessions で実際に統合するフェイク ChatWidget。"""

    def __init__(self, pool=(), only=None):
        self.pool = list(pool)
        self.only = only   # 採用 id をこの集合に制限する（一部だけを返すフェイク用）

    def merge_dataset_sessions(self, ds, incoming, *, prefer_incoming=False):
        for s in incoming:
            s.dataset = ds
        if self.only is not None:
            incoming = [s for s in incoming if s.id in self.only]
        new = chat_store.merge_sessions(
            self.pool, incoming, prefer_incoming=prefer_incoming)
        ids = {id(s) for s in new}
        self.pool = new
        return {s.id for s in incoming if id(s) in ids}

    def set_current_dataset(self, ds):
        pass


class _ChatDispatchWindow(_DispatchWindow):
    def __init__(self, cw):
        super().__init__()
        self._cw = cw

    def chat_widget(self):
        return self._cw

    def chat_sessions(self):
        return list(getattr(self._cw, "pool", []))

    def chat_merge_sessions(self, ds, sessions):
        return self._cw.merge_dataset_sessions(ds, sessions, prefer_incoming=True)

    def chat_deleted_sessions(self):
        return []

    def chat_clear_deleted(self, applied):
        pass


def _write_disk_chats(work_dir, *orders):
    out = []
    for o in orders:
        s = _chat("ds_a", f"o{o}")
        s.order = float(o)
        chat_store.write_session_file(work_dir, s)
        out.append(s)
    return out


def test_open_dataset_records_chat_baseline_only_for_adopted(ds_env):
    session._touched.clear()
    work_dir = dataset_config.get_work_dir("ds_a")
    x, y = _write_disk_chats(work_dir, 0, 1)
    cw = _MergingCW(only={y.id})
    session.open_dataset(_ChatDispatchWindow(cw), "ds_a")
    assert ("ds_a", x.id) not in session._chat_baseline
    base = session._chat_baseline[("ds_a", y.id)]
    assert base.work_dir == session._norm_dir(work_dir)
    assert base.fingerprint == _fp(y)
    assert base.order == 0.0   # chat_sessions() での位置（ディスクの order 1 ではない）

    session._chat_baseline.clear()

    class _NoneCW:
        pool: list = []

        def merge_dataset_sessions(self, ds, sessions):
            return None

    session.open_dataset(_ChatDispatchWindow(_NoneCW()), "ds_a")
    assert session._chat_baseline == {}


def test_open_dataset_chat_baseline_order_is_merged_position(ds_env):
    session._touched.clear()
    work_dir = dataset_config.get_work_dir("ds_a")
    y, x = _write_disk_chats(work_dir, 0, 1)   # Y が先頭（他の PC で新規）、X は 2 番目
    x_mem = copy.deepcopy(x)
    _set_base("ds_a", x, work_dir, 0.0)
    cw = _MergingCW(pool=[x_mem])
    win = _ChatDispatchWindow(cw)
    session.open_dataset(win, "ds_a")
    assert session._chat_baseline[("ds_a", y.id)].order == 1.0
    bx, by = _chat_bytes(work_dir, x.id), _chat_bytes(work_dir, y.id)
    session.save_all(win)
    assert _chat_bytes(work_dir, x.id) == bx
    assert _chat_bytes(work_dir, y.id) == by


def test_open_dataset_sparse_order_not_rewritten(ds_env):
    session._touched.clear()
    work_dir = dataset_config.get_work_dir("ds_a")
    x, z = _write_disk_chats(work_dir, 0, 5)
    win = _ChatDispatchWindow(_MergingCW())
    session.open_dataset(win, "ds_a")
    bx, bz = _chat_bytes(work_dir, x.id), _chat_bytes(work_dir, z.id)
    session.save_all(win)
    assert _chat_bytes(work_dir, x.id) == bx
    assert _chat_bytes(work_dir, z.id) == bz
    assert _read_disk(work_dir, z.id).order == 5.0


def test_open_dataset_then_reorder_elsewhere_not_clobbered(ds_env):
    session._touched.clear()
    work_dir = dataset_config.get_work_dir("ds_a")
    x, y = _write_disk_chats(work_dir, 0, 1)
    win = _ChatDispatchWindow(_MergingCW())
    session.open_dataset(win, "ds_a")
    # 他の PC の並べ替え（updated は変えない）
    chat_store.write_session_file(work_dir, _disk_copy(x, order=1.0))
    chat_store.write_session_file(work_dir, _disk_copy(y, order=0.0))
    bx, by = _chat_bytes(work_dir, x.id), _chat_bytes(work_dir, y.id)
    session.save_all(win)
    assert _chat_bytes(work_dir, x.id) == bx
    assert _chat_bytes(work_dir, y.id) == by
