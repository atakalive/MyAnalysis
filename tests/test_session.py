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
