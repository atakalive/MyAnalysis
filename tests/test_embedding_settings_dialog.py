"""gui/embedding_settings_dialog.py — 設定 → チャットの関連検索（実験的）…（Issue #126）。

導入・pip・索引は偽物に差し替える（ネットも推論も使わない）。``_spawn`` はクラスで同期にするので、
constructor の中の索引の状況の読み取りも同期で済む。"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import html
import tomllib

import pytest
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication

from common import embed_model as em
from common.i18n import tr
from common.paths import i18n_dir
from gui import embedding_settings_dialog as esd
from llm_bridge import chat_index as ci
from llm_bridge import chat_search
from llm_bridge import paths as lb_paths
from llm_bridge.paths import read_ui_pref, update_ui_pref

RURI = "ruri-v3-30m"
E5 = "multilingual-e5-small"


def _label(mid):
    return tr(em.CATALOG[mid].label_key)


class World:
    """偽の導入状態・索引・呼び出しの記録。"""

    def __init__(self):
        self.deps: list[str] = []
        self.models = {RURI: "missing", E5: "missing"}
        self.chunks = 0
        self.bytes = 0
        self.updated: float | None = None
        self.status_exc: Exception | None = None
        self.status_calls = 0
        self.calls: list[tuple] = []
        self.report = ci.UpdateReport(True, False)
        self.open_exc: Exception | None = None
        self.on_update = None
        self.clear_exc: Exception | None = None

    def readiness(self, mid):
        return em.Readiness(list(self.deps), self.models[mid], em.CATALOG[mid])

    def status(self):
        self.status_calls += 1
        if self.status_exc is not None:
            raise self.status_exc
        p = ci.load_prefs()
        shard = ci.ShardStatus("dsA", sessions=1, chunks=self.chunks, bytes=self.bytes,
                               updated=self.updated)
        return ci.IndexStatus(
            enabled=p.enabled, ready=False, model=p.model_id, deps_missing=list(self.deps),
            model_state=self.models[p.model_id], slug_dir="x", dims=em.CATALOG[p.model_id].dims,
            datasets=[shard], total_chunks=self.chunks, total_bytes=self.bytes,
            last_update=self.updated, stale={"dsA": 2})

    def open(self):
        if self.open_exc is not None:
            raise self.open_exc
        world = self

        class _Idx:
            def _run(self, kind, kw):
                world.calls.append((kind, kw))
                if world.on_update is not None:
                    world.on_update(kw)
                return world.report

            def update(self, **kw):
                return self._run("update", kw)

            def rebuild(self, **kw):
                return self._run("rebuild", kw)
        return _Idx()

    def clear(self, datasets=None, *, model_id=None, blocking=True):
        self.calls.append(("clear", {"datasets": datasets, "blocking": blocking}))
        if self.clear_exc is not None:
            raise self.clear_exc
        self.chunks = self.bytes = 0
        return 1


class FakeIndexer:
    def __init__(self, idle=True, running=False):
        self.idle = idle
        self.running = running
        self.calls: list[str] = []

    def wait_idle(self, timeout, cancelled=None):
        self.calls.append("wait_idle")
        return self.idle

    def is_running(self):
        return self.running


def _unexpected(name):
    def f(*a, **k):
        raise AssertionError(f"{name} must not be called")
    return f


@pytest.fixture(autouse=True)
def world(monkeypatch):
    QApplication.instance() or QApplication([])
    w = World()
    monkeypatch.setattr(esd.EmbeddingSettingsDialog, "_spawn", lambda self, fn: fn())
    monkeypatch.setattr(em, "readiness", w.readiness)
    monkeypatch.setattr(em, "missing_packages", lambda: list(w.deps))
    monkeypatch.setattr(em, "install", _unexpected("install"))
    monkeypatch.setattr(em, "uninstall", _unexpected("uninstall"))
    monkeypatch.setattr(em, "install_packages_worker", _unexpected("install_packages"))
    monkeypatch.setattr(ci, "status", w.status)
    monkeypatch.setattr(ci, "release_embedder", lambda: w.calls.append(("release",)))
    monkeypatch.setattr(ci, "clear", w.clear)
    monkeypatch.setattr(ci.ChatIndex, "open", classmethod(lambda cls, embedder=None: w.open()))
    return w


def _dialog(indexer=None):
    calls: list[int] = []
    dlg = esd.EmbeddingSettingsDialog(indexer=indexer, on_changed=lambda: calls.append(1))
    return dlg, calls


def _select(dlg, mid):
    dlg._model.setCurrentIndex(dlg._model.findData(mid))


def test_status_and_buttons_by_state(world):
    world.deps = ["onnxruntime", "tokenizers"]
    dlg, _ = _dialog()
    assert dlg.status_text() == tr("chat.recall.status.deps_missing",
                                   packages="onnxruntime, tokenizers")
    assert dlg._install_btn.text() == tr("chat.recall.btn.install_deps")
    assert not dlg._install_btn.isHidden() and dlg._uninstall_btn.isHidden()
    assert not dlg._enable.isEnabled() and not dlg._update_btn.isEnabled()
    world.deps = []
    dlg._refresh()
    assert dlg.status_text() == tr("chat.recall.status.model_missing", model=_label(RURI))
    assert dlg._install_btn.text() == tr("chat.recall.btn.install_model")
    world.models[RURI] = "broken"
    dlg._refresh()
    assert dlg.status_text() == tr("chat.recall.status.model_broken", model=_label(RURI))
    assert dlg._install_btn.text() == tr("chat.recall.btn.reinstall_model")
    assert not dlg._uninstall_btn.isHidden() and not dlg._enable.isEnabled()
    world.models[RURI] = "installed"
    dlg._refresh()
    assert dlg.status_text() == tr("chat.recall.status.index_empty", model=_label(RURI), dims=256)
    assert dlg._install_btn.isHidden() and not dlg._uninstall_btn.isHidden()
    assert dlg._enable.isEnabled()
    assert not dlg._update_btn.isEnabled() and not dlg._rebuild_btn.isEnabled()  # まだ無効
    assert not dlg._clear_btn.isEnabled()
    update_ui_pref("chat_recall_enabled", True)
    world.chunks, world.bytes, world.updated = 12, 3456, 1_700_000_000.0
    dlg._reload_index_status()
    assert dlg.status_text() == tr("chat.recall.status.ready", model=_label(RURI), dims=256,
                                   chunks=12, updated=chat_search.format_ts(1_700_000_000.0))
    assert dlg._update_btn.isEnabled() and dlg._rebuild_btn.isEnabled()
    assert dlg._clear_btn.isEnabled()
    assert dlg.index_text() == tr("chat.recall.index.line", model=_label(RURI), datasets=1,
                                  chunks=12, size=esd._mb(3456),
                                  updated=chat_search.format_ts(1_700_000_000.0), stale=2)
    dlg.deleteLater()


def test_index_line_before_first_update(world):
    dlg, _ = _dialog()
    assert dlg.index_text() == tr("chat.recall.index.line", model=_label(RURI), datasets=0,
                                  chunks=0, size=esd._mb(0), updated="—", stale=2)
    dlg.deleteLater()


def test_enable_toggle_saves_and_releases(world):
    world.models[RURI] = "installed"
    dlg, calls = _dialog()
    dlg._enable.setChecked(True)
    assert read_ui_pref("chat_recall_enabled") is True and calls
    assert ("release",) not in world.calls
    dlg._enable.setChecked(False)
    assert read_ui_pref("chat_recall_enabled") is False
    assert ("release",) in world.calls and len(calls) == 2
    dlg.deleteLater()


def test_enable_can_always_be_turned_off(world):
    update_ui_pref("chat_recall_enabled", True)        # モデルが消えた後（手で消したなど）
    dlg, _ = _dialog()
    assert dlg._enable.isChecked() and dlg._enable.isEnabled()
    dlg._enable.setChecked(False)
    assert not dlg._enable.isEnabled()                 # 揃っていないのでオンにはできない
    dlg.deleteLater()


def test_install_packages_then_refresh(world, monkeypatch):
    world.deps = ["onnxruntime", "tokenizers"]
    seen = {}

    def fake(progress, cancelled):
        progress("Collecting onnxruntime")
        seen["msg"] = dlg.message_text()
        world.deps = []
        return em.InstallResult(True)
    monkeypatch.setattr(em, "install_packages_worker", fake)
    dlg, calls = _dialog()
    dlg._on_install()
    assert seen["msg"] == tr("chat.recall.progress.pip", line="Collecting onnxruntime")
    assert dlg.message_text() == tr("chat.recall.done.deps")
    assert dlg.status_text() == tr("chat.recall.status.model_missing", model=_label(RURI))
    assert read_ui_pref("chat_recall_enabled", False) is False and not calls  # 自動では有効にしない
    assert not dlg.is_busy()
    dlg.deleteLater()


def test_install_packages_restart_and_failure(world, monkeypatch):
    world.deps = ["onnxruntime"]
    monkeypatch.setattr(em, "install_packages_worker", lambda p, c: em.InstallResult(True))
    dlg, _ = _dialog()
    dlg._on_install()
    assert dlg.message_text() == tr("chat.recall.done.deps_restart")    # 入れたのに見えない
    monkeypatch.setattr(em, "install_packages_worker", lambda p, c: em.InstallResult(False, "pip", "boom"))
    dlg._on_install()
    assert dlg.message_text() == tr("chat.recall.error.pip", message="boom")
    dlg.deleteLater()


def test_install_model_success_enables(world, monkeypatch):
    seen = {}

    def fake(mid, progress, cancelled):
        seen["mid"] = mid
        progress(10_000_000, 100_000_000)
        seen["msg"] = dlg.message_text()
        world.models[mid] = "installed"
        return em.InstallResult(True)
    monkeypatch.setattr(em, "install", fake)
    dlg, calls = _dialog()
    dlg._on_install()
    assert seen["mid"] == RURI
    assert seen["msg"] == tr("chat.recall.progress.bytes", done="10.0 MB", total="100.0 MB")
    assert read_ui_pref("chat_recall_model") == RURI and read_ui_pref("chat_recall_enabled") is True
    assert dlg._enable.isChecked() and calls
    assert dlg.message_text() == tr("chat.recall.done.installed") and not dlg.is_busy()
    dlg.deleteLater()


@pytest.mark.parametrize("code,key", [("network", "chat.recall.error.network"),
                                      ("integrity", "chat.recall.error.integrity")])
def test_install_model_failure_keeps_prefs(world, monkeypatch, code, key):
    monkeypatch.setattr(em, "install", lambda m, p, c: em.InstallResult(False, code, "x"))
    dlg, calls = _dialog()
    dlg._on_install()
    assert dlg.message_text() == tr(key, message="x")
    assert read_ui_pref("chat_recall_enabled", False) is False and not calls
    dlg.deleteLater()


def test_reinstall_broken_waits_and_releases(world, monkeypatch):
    world.models[RURI] = "broken"
    order = []
    monkeypatch.setattr(ci, "release_embedder", lambda: order.append("release"))
    monkeypatch.setattr(em, "install", lambda m, p, c: order.append("install") or
                        em.InstallResult(True))
    ix = FakeIndexer()
    dlg, _ = _dialog(ix)
    dlg._on_install()
    assert ix.calls == ["wait_idle"] and order == ["release", "install"]
    dlg.deleteLater()


def test_switching_to_missing_model_does_not_download(world):
    world.models[RURI] = "installed"
    update_ui_pref("chat_recall_enabled", True)
    dlg, calls = _dialog()
    assert dlg._update_btn.isEnabled()
    _select(dlg, E5)
    assert read_ui_pref("chat_recall_model", RURI) == RURI and not calls
    assert dlg._install_btn.text() == tr("chat.recall.btn.install_model")
    assert not dlg._install_btn.isHidden()
    assert not dlg._update_btn.isEnabled() and not dlg._rebuild_btn.isEnabled()
    assert _label(RURI) in dlg.index_text()           # 索引の行は使っているモデルのまま
    world.models[E5] = "installed"
    n = world.status_calls
    _select(dlg, RURI)
    _select(dlg, E5)
    assert read_ui_pref("chat_recall_model") == E5 and calls
    assert world.status_calls > n
    dlg.deleteLater()


def test_uninstall_waits_releases_and_disables(world, monkeypatch):
    world.models[RURI] = "installed"
    update_ui_pref("chat_recall_enabled", True)

    def fake(mid):
        world.calls.append(("uninstall", mid))
        world.models[mid] = "missing"
        return em.InstallResult(True)
    monkeypatch.setattr(em, "uninstall", fake)
    ix = FakeIndexer()
    dlg, calls = _dialog(ix)
    dlg._uninstall_btn.click()
    assert ix.calls == ["wait_idle"]
    assert world.calls[-2:] == [("release",), ("uninstall", RURI)]
    assert read_ui_pref("chat_recall_enabled") is False and not dlg._enable.isChecked()
    assert dlg.message_text() == tr("chat.recall.done.uninstalled") and calls
    assert not any(c[0] in ("clear", "update", "rebuild") for c in world.calls)  # 索引は消さない
    dlg.deleteLater()


def test_uninstall_other_model_keeps_enabled(world, monkeypatch):
    world.models[RURI] = "installed"
    world.models[E5] = "broken"
    update_ui_pref("chat_recall_enabled", True)
    monkeypatch.setattr(em, "uninstall", lambda mid: em.InstallResult(True))
    dlg, _ = _dialog()
    _select(dlg, E5)                                  # 壊れているので pref は変わらない
    dlg._uninstall_btn.click()
    assert read_ui_pref("chat_recall_enabled") is True and read_ui_pref("chat_recall_model",
                                                                         RURI) == RURI
    dlg.deleteLater()


def test_uninstall_in_use_and_not_idle(world, monkeypatch):
    world.models[RURI] = "installed"
    monkeypatch.setattr(em, "uninstall", lambda mid: em.InstallResult(False, "in_use"))
    dlg, _ = _dialog()
    dlg._uninstall_btn.click()
    assert dlg.message_text() == tr("chat.recall.error.in_use")
    dlg.deleteLater()
    monkeypatch.setattr(em, "uninstall", _unexpected("uninstall"))
    dlg, _ = _dialog(FakeIndexer(idle=False))
    dlg._uninstall_btn.click()
    assert dlg.message_text() == tr("chat.recall.error.locked")
    dlg.deleteLater()


def test_cannot_close_while_busy(world):
    world.models[RURI] = "installed"
    update_ui_pref("chat_recall_enabled", True)
    world.chunks = world.bytes = 5
    dlg, _ = _dialog()
    dlg._spawn = lambda fn: None                # 走ったまま
    dlg._on_index("update")
    assert dlg.is_busy()
    dlg.show()
    dlg.reject()
    assert dlg.isVisible()
    ev = QCloseEvent()
    dlg.closeEvent(ev)
    assert not ev.isAccepted()
    for w in (dlg._close_btn, dlg._install_btn, dlg._uninstall_btn, dlg._update_btn,
              dlg._rebuild_btn, dlg._clear_btn, dlg._enable, dlg._model, dlg._in_ai):
        assert not w.isEnabled()
    assert not dlg._cancel_btn.isHidden()
    dlg._cancel_btn.click()
    assert dlg._cancel.is_set()
    dlg._on_finished("index", ("update", em.InstallResult(False, "cancelled")))
    assert not dlg.is_busy() and dlg.message_text() == tr("chat.recall.error.cancelled")
    dlg.reject()
    assert not dlg.isVisible()
    dlg.deleteLater()


def test_origin_shows_everything(world):
    dlg, _ = _dialog()
    assert dlg._origin.openExternalLinks()
    text = dlg._origin.text()
    m = em.CATALOG[RURI]
    for s in (m.source, m.distribution, m.homepage, m.license_url, *em.PACKAGE_PAGES.values()):
        assert s in text
    for f in m.files:
        assert f.url in text and f.sha256 in text
    assert html.escape(esd._command_line(em.pip_install_argv())) in text
    assert html.escape(str(em.install_root())) in text
    assert html.escape(str(lb_paths.chat_index_dir())) in text
    assert esd._mb(sum(f.size for f in m.files)) in text
    assert tr("chat.recall.origin.privacy") in text
    _select(dlg, E5)
    text = dlg._origin.text()
    e5 = em.CATALOG[E5]
    assert all(f.url in text and f.sha256 in text for f in e5.files)
    assert m.files[0].url not in text and e5.license in text
    dlg.deleteLater()


def _ready(world):
    world.models[RURI] = "installed"
    update_ui_pref("chat_recall_enabled", True)


@pytest.mark.parametrize("kind,done_key", [("update", "chat.recall.done.index_updated"),
                                           ("rebuild", "chat.recall.done.index_rebuilt")])
def test_index_update_and_rebuild(world, kind, done_key):
    _ready(world)
    seen = {}

    def on_update(kw):
        kw["progress"]("dsA", 1, 2)
        seen["msg"] = dlg.message_text()
        seen["cancel"] = kw["cancel"]()
        world.chunks, world.bytes = 7, 70
    world.on_update = on_update
    dlg, calls = _dialog()
    n = world.status_calls
    (dlg._update_btn if kind == "update" else dlg._rebuild_btn).click()
    call_kind, kw = world.calls[-1]
    assert call_kind == kind and kw["blocking"] is False and kw["reload"] is False
    assert seen == {"msg": tr("chat.recall.progress.chunks", dataset="dsA", done=1, total=2),
                    "cancel": False}
    assert dlg.message_text() == tr(done_key) and not calls
    assert world.status_calls > n and "7" in dlg.index_text()
    dlg.deleteLater()


def test_index_progress_without_total_is_scanning(world):
    dlg, _ = _dialog()
    dlg._on_index_progress("", 0, 0)
    assert dlg._bar.maximum() == 0 and dlg.message_text() == tr("chat.recall.progress.scanning")
    dlg.deleteLater()


@pytest.mark.parametrize("setup,expected", [
    ("locked", ("chat.recall.error.locked", {})),
    ("cancelled", ("chat.recall.error.cancelled", {})),
    ("not_ready", ("chat.recall.error.not_ready", {})),
    ("boom", ("chat.recall.error.io", {"message": "RuntimeError: boom"})),
    ("not_idle", ("chat.recall.error.locked", {})),
])
def test_index_failures(world, setup, expected):
    _ready(world)
    ix = None
    if setup == "locked":
        world.report = ci.UpdateReport(False, False)
    elif setup == "cancelled":
        world.report = ci.UpdateReport(True, True)
    elif setup == "not_ready":
        world.open_exc = ci.NotReady("disabled")
    elif setup == "boom":
        world.open_exc = RuntimeError("boom")
    else:
        ix = FakeIndexer(idle=False)
    dlg, _ = _dialog(ix)
    dlg._update_btn.click()
    key, kw = expected
    assert dlg.message_text() == tr(key, **kw)
    if setup == "not_idle":
        assert not any(c[0] == "update" for c in world.calls)
    dlg.deleteLater()


def test_waiting_message_while_background_runs(world):
    _ready(world)
    dlg, _ = _dialog(FakeIndexer(running=True))
    dlg._spawn = lambda fn: None
    dlg._on_index("update")
    assert dlg.message_text() == tr("chat.recall.progress.waiting")
    dlg.deleteLater()


def test_clear_index(world):
    dlg, _ = _dialog()
    assert not dlg._clear_btn.isEnabled()            # 消すものが無い
    world.chunks, world.bytes = 3, 30
    dlg._reload_index_status()
    assert dlg._clear_btn.isEnabled()                # 無効のままでも消せる
    dlg._clear_btn.click()
    assert world.calls[-1] == ("clear", {"datasets": None, "blocking": False})
    assert dlg.message_text() == tr("chat.recall.done.index_cleared")
    assert not dlg._clear_btn.isEnabled()
    world.chunks, world.bytes = 3, 30
    world.clear_exc = ci.IndexLocked("locked: ruri-v3-30m")
    dlg._reload_index_status()
    dlg._clear_btn.click()
    assert dlg.message_text() == tr("chat.recall.error.locked")
    dlg.deleteLater()


@pytest.mark.parametrize("code,key,has_msg", [
    ("network", "chat.recall.error.network", True),
    ("integrity", "chat.recall.error.integrity", True),
    ("cancelled", "chat.recall.error.cancelled", False),
    ("io", "chat.recall.error.io", True),
    ("in_use", "chat.recall.error.in_use", False),
    ("pip", "chat.recall.error.pip", True),
    ("locked", "chat.recall.error.locked", False),
    ("not_ready", "chat.recall.error.not_ready", False),
    ("weird", "chat.recall.error.io", True),
    (None, "chat.recall.error.io", True),
])
def test_error_text_table(world, code, key, has_msg):
    dlg, _ = _dialog()
    res = em.InstallResult(False, code, "m")
    assert dlg._error_text(res) == (tr(key, message="m") if has_msg else tr(key))
    dlg.deleteLater()


def test_status_failure_is_shown(world):
    world.status_exc = OSError("boom")
    dlg, _ = _dialog()
    assert dlg.index_text() == tr("chat.recall.index.unavailable", message="OSError: boom")
    dlg.deleteLater()


def test_status_reads_coalesce(world):
    dlg, _ = _dialog()
    pending = []
    dlg._spawn = pending.append
    dlg._reload_index_status()
    dlg._reload_index_status()
    dlg._reload_index_status()
    assert len(pending) == 1 and dlg._status_again
    pending.pop()()                                  # 1 本目が終わる → まとめた 1 回を始める
    assert len(pending) == 1 and not dlg._status_again
    pending.pop()()
    assert pending == []
    dlg.deleteLater()


def test_status_during_busy_keeps_busy_and_message(world):
    _ready(world)
    dlg, _ = _dialog()
    pending = []
    dlg._spawn = pending.append
    dlg._on_index("update")
    msg = dlg.message_text()
    dlg._reload_index_status()
    status_work = pending.pop()
    world.chunks = 9
    status_work()
    assert dlg.is_busy() and dlg.message_text() == msg and not dlg._close_btn.isEnabled()
    assert "9" in dlg.index_text()
    dlg.deleteLater()


def test_status_after_close_is_dropped(world):
    dlg, _ = _dialog()
    before = dlg.index_text()
    pending = []
    dlg._spawn = pending.append
    world.chunks = 99
    dlg._reload_index_status()
    dlg.reject()
    pending.pop()()
    assert dlg.index_text() == before
    dlg._reload_index_status()
    assert pending == []                             # 閉じた後は読みに行かない
    dlg.deleteLater()


def test_in_ai_saved_and_unknown_model_falls_back(world):
    update_ui_pref("chat_recall_model", "no-such-model")
    dlg, _ = _dialog()
    assert dlg.selected_model() == em.DEFAULT_MODEL
    assert dlg._in_ai.isChecked()
    dlg._in_ai.setChecked(False)
    assert read_ui_pref("chat_recall_in_ai_search") is False
    dlg.deleteLater()


def test_catalog_label_keys_are_translated():
    for lang in ("ja", "en"):
        with open(i18n_dir() / f"{lang}.toml", "rb") as f:
            cat = tomllib.load(f)
        for m in em.CATALOG.values():
            assert m.label_key in cat, (lang, m.label_key)
