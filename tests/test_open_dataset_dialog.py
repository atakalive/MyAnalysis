"""Offscreen Qt tests for gui.open_dataset_dialog.OpenDatasetDialog (#50)."""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _meta(name, **kw):
    """Build a canned DatasetMeta. Defaults to a *complete* meta (all HEAVY
    fields present, uncomputed=False) so no background worker spawns — tests that
    exercise the worker set uncomputed=True explicitly."""
    from llm_bridge.dataset_meta import DatasetMeta
    m = DatasetMeta(name=name)
    m.available = kw.pop("available", True)
    m.unavailable_reason = kw.pop("unavailable_reason", None)
    m.uncomputed = kw.pop("uncomputed", False)
    defaults = {"disk_size_bytes": 1, "last_measurement": 1.0,
                "annotation_total": 1, "export_png_count": 1}
    for k, v in defaults.items():
        setattr(m, k, kw.pop(k, v))
    for k, v in kw.items():
        setattr(m, k, v)
    return m


class _FakeMain:
    """Minimal stand-in exposing _meta_workers (dialog registers workers here)."""
    def __init__(self):
        self._meta_workers = []


@pytest.fixture
def patch_picker(monkeypatch):
    """Return a setter that installs canned metas + stubs config."""
    import config
    from gui import open_dataset_dialog as mod

    def setup(metas):
        names = [m.name for m in metas]
        monkeypatch.setattr(config, "DATASETS", {n: {} for n in names})
        monkeypatch.setattr(config, "reload_datasets", lambda config_path=None: None)
        monkeypatch.setattr(mod.dataset_meta, "load_for_picker",
                            lambda ns: list(metas))
    return setup


def _make_dialog(qapp, main=None):
    from PySide6.QtWidgets import QWidget
    from gui.open_dataset_dialog import OpenDatasetDialog
    if main is None:
        main = _FakeMain()
    # parent must be a QWidget; give the dialog a real widget parent to avoid
    # top-level lifetime surprises in tests.
    holder = QWidget()
    dlg = OpenDatasetDialog(main, parent=holder)
    return dlg, main, holder


def test_row_count(qapp, patch_picker):
    patch_picker([_meta("a", uncomputed=False, disk_size_bytes=1,
                        last_measurement=1, annotation_total=1, export_png_count=1),
                  _meta("b", uncomputed=False, disk_size_bytes=1,
                        last_measurement=1, annotation_total=1, export_png_count=1)])
    dlg, _m, _h = _make_dialog(qapp)
    assert dlg._proxy.rowCount() == 2


def test_filter(qapp, patch_picker):
    patch_picker([_meta("alpha"), _meta("beta"), _meta("gamma")])
    dlg, _m, _h = _make_dialog(qapp)
    dlg._proxy.set_needle("bet")
    assert dlg._proxy.rowCount() == 1


def test_unavailable_disables_open(qapp, patch_picker):
    patch_picker([_meta("bad", available=False, unavailable_reason="no-host")])
    dlg, _m, _h = _make_dialog(qapp)
    assert dlg._open_btn.isEnabled() is False
    assert dlg._edit_btn.isEnabled() is False


def test_selected_dataset(qapp, patch_picker):
    patch_picker([_meta("only")])
    dlg, _m, _h = _make_dialog(qapp)
    assert dlg.selected_dataset() == "only"


def test_double_click_accepts(qapp, patch_picker):
    patch_picker([_meta("only")])
    dlg, _m, _h = _make_dialog(qapp)
    accepted = {"v": False}
    dlg.accepted.connect(lambda: accepted.__setitem__("v", True))
    dlg._accept()
    assert accepted["v"] is True


def test_refresh_runs_heavy_on_worker_thread(qapp, patch_picker, monkeypatch):
    # [更新] must route the HEAVY rebuild through the background worker — never
    # run compute_meta's os.walk inline on the GUI thread (reviewer/reviewer P1).
    import threading

    from gui import open_dataset_dialog as mod
    patch_picker([_meta("a", uncomputed=False)])   # complete meta → no startup worker
    main_ident = threading.get_ident()
    calls = []

    def fake_rebuild(ds, *, heavy=True, should_stop=None):
        calls.append((ds, heavy, should_stop is not None, threading.get_ident()))

    monkeypatch.setattr(mod.dataset_meta, "rebuild_meta", fake_rebuild)
    monkeypatch.setattr(mod.dataset_meta, "load_one",
                        lambda n: _meta(n, uncomputed=False))
    dlg, _m, _h = _make_dialog(qapp)
    assert dlg._workers == []                       # no worker before refresh
    dlg._on_refresh()
    assert len(dlg._workers) == 1                   # queued, not run inline
    worker = dlg._workers[-1]
    assert worker.wait(5000)                        # let the background run finish
    assert len(calls) == 1
    ds, heavy, has_stop, ident = calls[0]
    assert (ds, heavy, has_stop) == ("a", True, True)   # heavy + cancellable
    assert ident != main_ident                      # ran off the GUI thread


def test_default_order_is_mru_not_name(qapp, patch_picker):
    # 'zeta' most recently opened → should sort first despite alpha order.
    patch_picker([
        _meta("alpha", last_opened=None, uncomputed=False, disk_size_bytes=1,
              last_measurement=1, annotation_total=1, export_png_count=1),
        _meta("zeta", last_opened=1000.0, uncomputed=False, disk_size_bytes=1,
              last_measurement=1, annotation_total=1, export_png_count=1),
    ])
    dlg, _m, _h = _make_dialog(qapp)
    from PySide6.QtCore import Qt
    first = dlg._proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole)
    assert first == "zeta"      # MRU preserved; not re-sorted to 'alpha'


def test_column_sort_none_sentinel(qapp, patch_picker):
    from PySide6.QtCore import Qt
    patch_picker([
        _meta("a", last_touched=100.0, uncomputed=False, disk_size_bytes=1,
              last_measurement=1, annotation_total=1, export_png_count=1),
        _meta("b", last_touched=None, uncomputed=False, disk_size_bytes=1,
              last_measurement=1, annotation_total=1, export_png_count=1),
    ])
    dlg, _m, _h = _make_dialog(qapp)
    from gui.open_dataset_dialog import COL_TOUCHED
    dlg._proxy.sort(COL_TOUCHED, Qt.SortOrder.DescendingOrder)
    # None → -inf sentinel → 'b' ends up last on descending.
    last = dlg._proxy.index(dlg._proxy.rowCount() - 1, 0).data(Qt.ItemDataRole.DisplayRole)
    assert last == "b"


def test_type_broken_meta_all_shown(qapp, patch_picker):
    # simulate a meta whose last_touched survived as a str (shouldn't happen after
    # from_dict, but the dialog must not crash if one slips through as None).
    patch_picker([_meta("a", last_touched=None), _meta("b", last_touched=5.0)])
    dlg, _m, _h = _make_dialog(qapp)
    assert dlg._proxy.rowCount() == 2


def test_worker_parent_and_registry(qapp, patch_picker, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from gui import open_dataset_dialog as mod
    # stale row triggers the worker
    patch_picker([_meta("a", uncomputed=True)])
    monkeypatch.setattr(mod.dataset_meta, "rebuild_meta",
                        lambda *a, **k: None)
    monkeypatch.setattr(mod.dataset_meta, "load_one", lambda n: _meta(n))
    main = _FakeMain()
    dlg, _m, _h = _make_dialog(qapp, main=main)
    assert len(dlg._workers) == 1
    worker = dlg._workers[-1]
    # Qt parent is the long-lived QApplication (NOT the short-lived dialog),
    # and the worker is also held in the main window's registry.
    assert worker.parent() is QApplication.instance()
    assert worker in main._meta_workers
    # close should request interruption but not destroy
    dlg._interrupt_workers()
    assert worker.isInterruptionRequested()
    worker.wait(2000)


def test_refresh_regrabs_live_thumbnail_when_open(qapp, patch_picker, monkeypatch,
                                                  tmp_path):
    # 更新 on an open dataset re-grabs its live active tab via the window and shows
    # that fresh PNG (precedence over the persisted meta.thumbnail).
    from PySide6.QtGui import QImage

    from gui import open_dataset_dialog as mod
    patch_picker([_meta("a", uncomputed=False)])       # complete meta → no startup worker
    monkeypatch.setattr(mod.dataset_meta, "rebuild_meta", lambda *a, **k: None)
    monkeypatch.setattr(mod.dataset_meta, "load_one",
                        lambda n: _meta(n, uncomputed=False))

    png = tmp_path / "current_view.png"
    img = QImage(4, 4, QImage.Format.Format_RGB32)
    img.fill(0)
    assert img.save(str(png), "PNG")

    class _Main(_FakeMain):
        def __init__(self):
            super().__init__()
            self.calls = []

        def snapshot_active_thumbnail(self, dataset):
            self.calls.append(dataset)
            return str(png)

    main = _Main()
    dlg, _m, _h = _make_dialog(qapp, main=main)
    dlg._on_refresh()
    assert main.calls == ["a"]                          # window asked to re-grab
    assert dlg._live_thumbs.get("a") == str(png)        # live path recorded, wins
    assert not dlg._thumb.pixmap().isNull()             # thumbnail rendered
    if dlg._workers:
        dlg._workers[-1].wait(5000)


def test_refresh_no_regrab_when_window_lacks_hook(qapp, patch_picker, monkeypatch):
    # A window without snapshot_active_thumbnail (e.g. headless fake) just runs the
    # metadata worker — no crash, no live thumbnail recorded.
    from gui import open_dataset_dialog as mod
    patch_picker([_meta("a", uncomputed=False)])
    monkeypatch.setattr(mod.dataset_meta, "rebuild_meta", lambda *a, **k: None)
    monkeypatch.setattr(mod.dataset_meta, "load_one",
                        lambda n: _meta(n, uncomputed=False))
    dlg, _m, _h = _make_dialog(qapp)                    # _FakeMain has no hook
    dlg._on_refresh()
    assert dlg._live_thumbs == {}
    assert len(dlg._workers) == 1
    dlg._workers[-1].wait(5000)


def test_description_label_wraps(qapp, patch_picker):
    # A long 概要 must render with word-wrap so it wraps inside the detail pane
    # instead of forcing the dialog's minimum width wider than resize(900, 500)
    # and blowing the window out horizontally.
    from PySide6.QtWidgets import QLabel
    long_desc = "あ" * 500
    # Complete meta (all HEAVY fields present) so no background worker spawns.
    patch_picker([_meta("a", description=long_desc, uncomputed=False,
                        disk_size_bytes=1, last_measurement=1,
                        annotation_total=1, export_png_count=1)])
    dlg, _m, _h = _make_dialog(qapp)
    # The description text lives only in the detail pane's QLabel (the table cell
    # is model data, not a widget), so match on text to locate it robustly.
    matches = [w for w in dlg.findChildren(QLabel) if w.text() == long_desc]
    assert matches, "description label not found in detail pane"
    assert all(w.wordWrap() for w in matches)


def test_edit_desc_failure_warns(qapp, patch_picker, monkeypatch):
    from gui import open_dataset_dialog as mod
    patch_picker([_meta("a")])
    monkeypatch.setattr(mod.QInputDialog, "getMultiLineText",
                        staticmethod(lambda *a, **k: ("newdesc", True)))
    monkeypatch.setattr(mod.dataset_meta, "patch_description",
                        lambda ds, text: (_ for _ in ()).throw(RuntimeError("gone")))
    warned = {"v": False}
    monkeypatch.setattr(mod.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warned.__setitem__("v", True)))
    dlg, _m, _h = _make_dialog(qapp)
    dlg._on_edit_desc()
    assert warned["v"] is True


# ---- Issue #91: completed flag + completed section ----

def _patch_toggle(monkeypatch, state):
    """Install patch_completed/load_one stubs backed by `state` (name→bool)."""
    from gui import open_dataset_dialog as mod
    monkeypatch.setattr(mod.dataset_meta, "patch_completed",
                        lambda ds, val: state.__setitem__(ds, bool(val)))
    monkeypatch.setattr(mod.dataset_meta, "load_one",
                        lambda n: _meta(n, completed=state.get(n, False)))


def test_completed_splits_into_two_sections(qapp, patch_picker):
    patch_picker([_meta("a"), _meta("done", completed=True)])
    dlg, _m, _h = _make_dialog(qapp)
    assert dlg._proxy.rowCount() == 1
    assert dlg._completed_proxy.rowCount() == 1


def test_completed_section_collapsed_by_default(qapp, patch_picker):
    patch_picker([_meta("a"), _meta("done", completed=True)])
    dlg, _m, _h = _make_dialog(qapp)
    assert dlg._completed_toggle.isChecked() is False
    assert dlg._completed_view.isHidden() is True
    assert dlg._completed_toggle.isHidden() is False   # header shown (n=1)


def test_completed_toggle_expands(qapp, patch_picker):
    from PySide6.QtCore import Qt
    patch_picker([_meta("a"), _meta("done", completed=True)])
    dlg, _m, _h = _make_dialog(qapp)
    dlg._completed_toggle.setChecked(True)
    assert dlg._completed_view.isHidden() is False
    assert dlg._completed_toggle.arrowType() == Qt.ArrowType.DownArrow
    dlg._completed_toggle.setChecked(False)
    assert dlg._completed_view.isHidden() is True
    assert dlg._completed_toggle.arrowType() == Qt.ArrowType.RightArrow


def test_completed_header_hidden_when_none(qapp, patch_picker):
    patch_picker([_meta("a"), _meta("b")])
    dlg, _m, _h = _make_dialog(qapp)
    assert dlg._completed_toggle.isHidden() is True
    assert dlg._completed_view.isHidden() is True


def test_header_count_tracks_needle(qapp, patch_picker):
    patch_picker([_meta("a"), _meta("xdone", completed=True),
                  _meta("ydone", completed=True)])
    dlg, _m, _h = _make_dialog(qapp)
    assert "2" in dlg._completed_toggle.text()
    dlg._filter.setText("xdone")
    assert "1" in dlg._completed_toggle.text()


def test_unmark_last_completed_while_expanded_hides_section(qapp, patch_picker,
                                                            monkeypatch):
    state = {"done": True}
    _patch_toggle(monkeypatch, state)
    patch_picker([_meta("a"), _meta("done", completed=True)])
    dlg, _m, _h = _make_dialog(qapp)
    dlg._completed_toggle.setChecked(True)
    dlg._completed_view.selectRow(0)                 # select the completed row
    assert dlg._current_meta().name == "done"
    dlg._on_toggle_completed()                       # unmark → moves up
    assert dlg._completed_proxy.rowCount() == 0
    assert dlg._completed_toggle.isHidden() is True
    assert dlg._completed_view.isHidden() is True


def test_needle_matches_completed_only_auto_expands(qapp, patch_picker):
    patch_picker([_meta("alpha"), _meta("zbeta", completed=True)])
    dlg, _m, _h = _make_dialog(qapp)
    dlg._filter.setText("zbeta")
    assert dlg._completed_toggle.isChecked() is True
    assert dlg._active_view is dlg._completed_view
    assert dlg._current_meta().name == "zbeta"


def test_needle_clear_keeps_completed_selection(qapp, patch_picker):
    patch_picker([_meta("alpha"), _meta("zbeta", completed=True)])
    dlg, _m, _h = _make_dialog(qapp)
    dlg._filter.setText("zbeta")
    assert dlg._current_meta().name == "zbeta"
    dlg._filter.setText("")                          # top list returns
    assert dlg._proxy.rowCount() == 1
    assert dlg._current_meta().name == "zbeta"       # selection stays on completed


def test_selection_filtered_out_disables_buttons(qapp, patch_picker):
    patch_picker([_meta("alpha"), _meta("beta")])
    dlg, _m, _h = _make_dialog(qapp)
    dlg._view.selectRow(0)
    assert dlg._current_meta() is not None
    dlg._filter.setText("nomatch")
    assert dlg._current_meta() is None
    assert dlg._open_btn.isEnabled() is False
    assert dlg._edit_btn.isEnabled() is False
    assert dlg._complete_btn.isEnabled() is False


def test_mark_moves_down_collapsed(qapp, patch_picker, monkeypatch):
    state = {"a": False, "b": False}
    _patch_toggle(monkeypatch, state)
    patch_picker([_meta("a"), _meta("b")])
    dlg, _m, _h = _make_dialog(qapp)
    dlg._view.selectRow(0)                           # select "a"
    dlg._on_toggle_completed()                       # mark completed
    assert state["a"] is True
    assert dlg._completed_proxy.rowCount() == 1
    assert dlg._proxy.rowCount() == 1
    assert dlg._active_view is dlg._view             # collapsed → stays on top list
    assert dlg._current_meta().name == "b"


def test_unmark_moves_up(qapp, patch_picker, monkeypatch):
    state = {"a": True, "b": False}
    _patch_toggle(monkeypatch, state)
    patch_picker([_meta("a", completed=True), _meta("b")])
    dlg, _m, _h = _make_dialog(qapp)
    dlg._completed_toggle.setChecked(True)
    dlg._completed_view.selectRow(0)                 # select "a" in completed
    dlg._on_toggle_completed()                       # unmark → up
    assert state["a"] is False
    assert dlg._proxy.rowCount() == 2
    assert dlg._active_view is dlg._view
    assert dlg._current_meta().name == "a"


def test_mark_last_top_row_clears_selection(qapp, patch_picker, monkeypatch):
    state = {"only": False}
    _patch_toggle(monkeypatch, state)
    patch_picker([_meta("only")])
    dlg, _m, _h = _make_dialog(qapp)
    dlg._view.selectRow(0)
    dlg._on_toggle_completed()                       # top list becomes empty
    assert dlg._proxy.rowCount() == 0
    assert dlg._current_meta() is None
    assert dlg._open_btn.isEnabled() is False
    assert dlg._edit_btn.isEnabled() is False
    assert dlg._complete_btn.isEnabled() is False


def test_collapse_hides_completed_selection(qapp, patch_picker):
    patch_picker([_meta("a"), _meta("done", completed=True)])
    dlg, _m, _h = _make_dialog(qapp)
    dlg._completed_toggle.setChecked(True)
    dlg._completed_view.selectRow(0)
    assert dlg._current_meta().name == "done"
    dlg._completed_toggle.setChecked(False)          # collapse
    assert dlg._active_view is dlg._view
    m = dlg._current_meta()
    assert m is not None                             # top list has "a"
    assert m.name == "a"                             # never returns hidden completed row


def test_collapse_with_empty_top_clears_selection(qapp, patch_picker):
    patch_picker([_meta("done", completed=True)])
    dlg, _m, _h = _make_dialog(qapp)
    # auto-expanded at construction (top empty, completed present)
    assert dlg._completed_toggle.isChecked() is True
    assert dlg._current_meta().name == "done"
    dlg._completed_toggle.setChecked(False)          # collapse, top empty
    assert dlg._active_view is dlg._view
    assert dlg._current_meta() is None
    assert dlg._open_btn.isEnabled() is False


def test_needle_matches_both_sections(qapp, patch_picker):
    patch_picker([_meta("shared_a"), _meta("shared_done", completed=True)])
    dlg, _m, _h = _make_dialog(qapp)
    dlg._filter.setText("shared")
    assert dlg._proxy.rowCount() == 1
    assert dlg._completed_proxy.rowCount() == 1


def test_completed_double_click_accepts(qapp, patch_picker):
    patch_picker([_meta("a"), _meta("done", completed=True)])
    dlg, _m, _h = _make_dialog(qapp)
    dlg._completed_toggle.setChecked(True)
    dlg._completed_view.selectRow(0)
    accepted = {"v": False}
    dlg.accepted.connect(lambda: accepted.__setitem__("v", True))
    dlg._accept()
    assert accepted["v"] is True
    assert dlg.selected_dataset() == "done"


def test_two_views_mutually_exclusive_selection(qapp, patch_picker):
    patch_picker([_meta("a"), _meta("done", completed=True)])
    dlg, _m, _h = _make_dialog(qapp)
    dlg._completed_toggle.setChecked(True)
    dlg._view.selectRow(0)
    assert dlg._active_view is dlg._view
    dlg._completed_view.selectRow(0)
    assert dlg._active_view is dlg._completed_view
    assert not dlg._view.selectionModel().selectedRows()   # top cleared


def test_complete_button_label_switches(qapp, patch_picker):
    from common.i18n import tr
    patch_picker([_meta("a"), _meta("done", completed=True)])
    dlg, _m, _h = _make_dialog(qapp)
    dlg._view.selectRow(0)                            # not completed
    assert dlg._complete_btn.text() == tr("picker.btn.mark_completed")
    dlg._completed_toggle.setChecked(True)
    dlg._completed_view.selectRow(0)                 # completed
    assert dlg._complete_btn.text() == tr("picker.btn.unmark_completed")


def test_completed_badge_shown(qapp, patch_picker):
    from PySide6.QtWidgets import QLabel
    from common.i18n import tr
    patch_picker([_meta("done", completed=True)])
    dlg, _m, _h = _make_dialog(qapp)
    labels = [w.text() for w in dlg.findChildren(QLabel)]
    assert tr("picker.badge.completed") in labels


def test_toggle_completed_failure_warns(qapp, patch_picker, monkeypatch):
    from gui import open_dataset_dialog as mod
    patch_picker([_meta("a")])
    monkeypatch.setattr(mod.dataset_meta, "patch_completed",
                        lambda ds, val: (_ for _ in ()).throw(RuntimeError("gone")))
    warned = {"v": False}
    monkeypatch.setattr(mod.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warned.__setitem__("v", True)))
    dlg, _m, _h = _make_dialog(qapp)
    dlg._view.selectRow(0)
    dlg._on_toggle_completed()
    assert warned["v"] is True


def test_all_completed_auto_expands(qapp, patch_picker):
    patch_picker([_meta("x", completed=True), _meta("y", completed=True)])
    dlg, _m, _h = _make_dialog(qapp)
    assert dlg._proxy.rowCount() == 0
    assert dlg._completed_toggle.isChecked() is True
    assert dlg._active_view is dlg._completed_view
    assert dlg._current_meta() is not None
