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


def test_refresh_calls_heavy(qapp, patch_picker, monkeypatch):
    from gui import open_dataset_dialog as mod
    patch_picker([_meta("a", uncomputed=False, disk_size_bytes=1,
                        last_measurement=1, annotation_total=1, export_png_count=1)])
    calls = []
    monkeypatch.setattr(mod.dataset_meta, "rebuild_meta",
                        lambda ds, *, heavy=True, should_stop=None:
                        calls.append((ds, heavy)))
    monkeypatch.setattr(mod.dataset_meta, "load_one",
                        lambda n: _meta(n, uncomputed=False))
    dlg, _m, _h = _make_dialog(qapp)
    dlg._on_refresh()
    assert calls == [("a", True)]


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
    assert dlg._worker is not None
    assert dlg._worker.parent() is QApplication.instance()
    assert dlg._worker in main._meta_workers
    # close should request interruption but not destroy
    dlg._interrupt_worker()
    assert dlg._worker.isInterruptionRequested()
    dlg._worker.wait(2000)


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
