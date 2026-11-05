"""ChatIndexer（gui/chat_indexer.py）と ToolWindow への差し込み（Issue #126）。

索引の本体（``llm_bridge.chat_index``）は ``is_ready`` と ``ChatIndex.open`` を偽物に差し替える。偽の
``update`` はゲートで止めておける。worker を走らせるテストは reap_indexers fixture の teardown（成否に
関わらず走る）でゲートを開け、stop し、worker スレッドを join して生存していないことを確かめる。
"""
from __future__ import annotations

import ast
import json
import threading
import time
from pathlib import Path
from typing import ClassVar

import pytest

from gui import chat_indexer as gci
from gui.chat_indexer import THREAD_NAME, ChatIndexer
from llm_bridge import chat_index as ci

REPO = Path(__file__).resolve().parent.parent


def _wait_until(pred, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for condition")
        time.sleep(0.005)


class FakeIndex:
    """ChatIndex の代役。update はゲートが開くまで戻らない。"""

    def __init__(self) -> None:
        self.ready = True
        self.gate = threading.Event()
        self.gate.set()
        self.started = threading.Event()
        self.lock = threading.Lock()
        self.opens = 0
        self.calls: list[dict] = []
        self.threads: list[threading.Thread] = []
        self.cancel_seen: list[bool] = []
        self.raise_exc: Exception | None = None
        self.ran = True

    def is_ready(self) -> bool:
        return self.ready

    def open(self):
        with self.lock:
            self.opens += 1
        return self

    def update(self, **kw):
        with self.lock:
            self.calls.append(kw)
            self.threads.append(threading.current_thread())
        self.started.set()
        self.gate.wait(10.0)
        c = bool(kw["cancel"]())
        with self.lock:
            self.cancel_seen.append(c)
        if self.raise_exc is not None:
            raise self.raise_exc
        return ci.UpdateReport(self.ran, c)

    def n(self) -> int:
        with self.lock:
            return len(self.calls)


@pytest.fixture
def reap_indexers(monkeypatch):
    indexers: list[ChatIndexer] = []
    fakes: list[FakeIndex] = []
    orig_init = ChatIndexer.__init__

    def tracking_init(self, *a, **k):
        orig_init(self, *a, **k)
        indexers.append(self)

    monkeypatch.setattr(ChatIndexer, "__init__", tracking_init)

    def new_fake() -> FakeIndex:
        f = FakeIndex()
        fakes.append(f)
        monkeypatch.setattr(ci, "is_ready", f.is_ready)
        monkeypatch.setattr(ci.ChatIndex, "open", classmethod(lambda cls, embedder=None: f.open()))
        return f

    yield new_fake
    for f in fakes:
        f.gate.set()
    for ix in indexers:
        ix.stop(wait_s=5.0)
    leftovers = [t for t in threading.enumerate() if t.name == THREAD_NAME]
    for t in leftovers:
        t.join(5.0)
    assert not [t for t in leftovers if t.is_alive()]


@pytest.fixture
def fake(reap_indexers, monkeypatch):
    monkeypatch.setattr(ChatIndexer, "DEBOUNCE_S", 0.05)
    monkeypatch.setattr(ChatIndexer, "DEFAULT_WAIT_S", 0.2)
    return reap_indexers()


def _idle(ix: ChatIndexer) -> bool:
    with ix._cv:
        return not ix._alive and not ix._active


# --------------------------------------------------------------------------- #
# ChatIndexer
# --------------------------------------------------------------------------- #
def test_off_creates_no_thread(fake):
    fake.ready = False
    ix = ChatIndexer()
    assert ix.request_update("save") is False
    assert ix._thread is None and not ix.has_pending()


def test_requests_coalesce_into_one_nonblocking_update(fake):
    ix = ChatIndexer()
    for _ in range(5):
        assert ix.request_update("save") is True
    _wait_until(lambda: fake.n() == 1 and _idle(ix))
    time.sleep(0.15)
    assert fake.n() == 1
    kw = fake.calls[0]
    assert kw["blocking"] is False and kw["reload"] is False and kw["progress"] is None
    t = fake.threads[0]
    assert t.name == THREAD_NAME and t.daemon


def test_each_request_extends_the_deadline(fake, monkeypatch):
    monkeypatch.setattr(ChatIndexer, "DEBOUNCE_S", 5.0)
    ix = ChatIndexer()
    ix.request_update("a")
    d1 = ix._deadline
    time.sleep(0.02)
    ix.request_update("b")
    assert ix._deadline > d1 and fake.n() == 0


def test_requests_during_a_run_coalesce_into_one_followup(fake):
    fake.gate.clear()
    ix = ChatIndexer()
    ix.request_update("a")
    assert fake.started.wait(5.0)
    assert ix.is_running()
    ix.request_update("b")
    ix.request_update("c")
    assert ix.has_pending()
    fake.gate.set()
    _wait_until(lambda: fake.n() == 2 and _idle(ix))
    time.sleep(0.15)
    assert fake.n() == 2


def test_disabled_during_debounce_does_not_open(fake, monkeypatch):
    monkeypatch.setattr(ChatIndexer, "DEBOUNCE_S", 0.15)
    ix = ChatIndexer()
    ix.request_update("a")
    fake.ready = False
    _wait_until(lambda: _idle(ix))
    assert fake.opens == 0 and fake.n() == 0


def test_failure_is_logged_and_next_request_runs(fake, caplog):
    fake.raise_exc = RuntimeError("boom")
    ix = ChatIndexer()
    with caplog.at_level("DEBUG", logger=gci.__name__):
        ix.request_update("a")
        _wait_until(lambda: fake.n() == 1 and _idle(ix))
    assert any("background chat index update failed" in r.message for r in caplog.records)
    fake.raise_exc = None
    ix.request_update("b")
    _wait_until(lambda: fake.n() == 2 and _idle(ix))


def test_locked_is_left_until_the_next_request(fake):
    fake.ran = False
    ix = ChatIndexer()
    ix.request_update("a")
    _wait_until(lambda: fake.n() == 1 and _idle(ix))
    time.sleep(0.15)
    assert fake.n() == 1 and not ix.has_pending()


def test_hold_during_debounce_defers_until_release(fake, monkeypatch):
    monkeypatch.setattr(ChatIndexer, "DEBOUNCE_S", 0.1)
    ix = ChatIndexer()
    ix.request_update("a")
    ix.hold()
    time.sleep(0.3)
    assert fake.n() == 0 and ix.has_pending()
    ix.release()
    _wait_until(lambda: fake.n() == 1 and _idle(ix))


def test_hold_cancels_the_running_update_and_remembers_it(fake):
    fake.gate.clear()
    ix = ChatIndexer()
    ix.request_update("a")
    assert fake.started.wait(5.0)
    assert not ix._should_cancel()
    ix.hold()
    assert ix._should_cancel()
    ix.release()                       # 中止がまだ届いていないうちに外す
    assert ix._should_cancel()         # その回が終わるまで覚えている
    fake.gate.set()
    _wait_until(lambda: fake.n() == 2 and _idle(ix))   # release が先 → 中止の完了で予約し直す
    assert fake.cancel_seen == [True, False]


def test_cancel_finishing_while_held_reruns_after_release(fake):
    fake.gate.clear()
    ix = ChatIndexer()
    ix.request_update("a")
    assert fake.started.wait(5.0)
    ix.hold()
    fake.gate.set()
    _wait_until(lambda: not ix.is_running())
    time.sleep(0.2)
    assert fake.n() == 1 and ix.has_pending()           # hold 中は走り直さない
    ix.release()                                        # 中止の完了が先 → release で予約する
    _wait_until(lambda: fake.n() == 2 and _idle(ix))
    assert fake.cancel_seen == [True, False]


def test_request_while_held_runs_once_after_release(fake):
    ix = ChatIndexer()
    ix.hold()
    ix.hold()
    assert ix.request_update("settings") is True
    assert ix._thread is None
    ix.release()
    time.sleep(0.15)
    assert fake.n() == 0                                # まだ 1 つ hold が残っている
    ix.release()
    ix.release()                                        # 多すぎる release は数を負にしない
    assert ix._held == 0
    _wait_until(lambda: fake.n() == 1 and _idle(ix))
    time.sleep(0.15)
    assert fake.n() == 1


def test_wait_idle(fake):
    ix = ChatIndexer()
    assert ix.wait_idle(0.01) is True
    fake.gate.clear()
    ix.request_update("a")
    assert fake.started.wait(5.0)
    ix.hold()
    assert ix.wait_idle(0.05) is False                  # 期限切れ
    assert ix.wait_idle(5.0, lambda: True) is False     # 中止
    threading.Timer(0.05, fake.gate.set).start()
    assert ix.wait_idle(5.0) is True
    ix.release()


def test_stop_is_idempotent_and_rejects_requests(fake):
    ix = ChatIndexer()
    assert ix.stop() is True
    assert ix.stop() is True
    assert ix.request_update("a") is False
    ix.hold()
    ix.release()
    assert ix._thread is None


def test_stop_wakes_a_waiting_worker(fake, monkeypatch):
    monkeypatch.setattr(ChatIndexer, "DEBOUNCE_S", 30.0)
    ix = ChatIndexer()
    ix.request_update("a")
    t0 = time.monotonic()
    assert ix.stop(wait_s=5.0) is True
    assert time.monotonic() - t0 < 2.0 and fake.n() == 0


def test_stop_cancels_running_update_and_is_bounded(fake):
    fake.gate.clear()
    ix = ChatIndexer()
    ix.request_update("a")
    assert fake.started.wait(5.0)
    t0 = time.monotonic()
    assert ix.stop() is False                           # DEFAULT_WAIT_S（0.2 s）で諦める
    assert time.monotonic() - t0 < 2.0
    assert ix._should_cancel()
    fake.gate.set()
    _wait_until(lambda: _idle(ix))
    assert fake.cancel_seen == [True] and fake.n() == 1


def _imports(tree: ast.AST) -> set[str]:
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
    return out


def test_module_is_qt_free_and_imports_the_index_lazily():
    tree = ast.parse((REPO / "gui" / "chat_indexer.py").read_text(encoding="utf-8"))
    mods = _imports(tree)
    assert not any(m.split(".")[0] in ("PySide6", "gui") for m in mods)
    top = set()
    for n in tree.body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            top |= _imports(n)
    assert "llm_bridge" not in top and "llm_bridge.chat_index" not in top


def test_qt_integration_stops_chat_indexer():
    src = (REPO / "devtools" / "qt_integration.py").read_text(encoding="utf-8")
    assert '"_stop_chat_indexer"' in src


# --------------------------------------------------------------------------- #
# ToolWindow
# --------------------------------------------------------------------------- #
@pytest.fixture
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(qapp):
    from gui.window import ToolWindow
    w = ToolWindow()
    yield w
    w._stop_chat_indexer()
    w.deleteLater()


def _record(monkeypatch, win) -> list[str]:
    reasons: list[str] = []
    monkeypatch.setattr(win, "request_chat_index_update", lambda r: reasons.append(r) or True)
    return reasons


def test_off_never_builds_the_indexer(win, monkeypatch):
    def boom(self):
        raise AssertionError("ChatIndexer must not be built while the feature is off")
    monkeypatch.setattr(ChatIndexer, "__init__", boom)
    win.set_session_saver(lambda: ([], []))
    win._save_session()
    win.register_command("open-dataset", lambda name: "restored:0")
    win._open_dataset_by_name("dsA")
    assert win.request_chat_index_update("save") is False
    assert getattr(win, "_chat_indexer", None) is None
    assert not [t for t in threading.enumerate() if t.name == THREAD_NAME]


def test_on_request_goes_to_the_indexer(win, fake):
    from llm_bridge.paths import update_ui_pref
    update_ui_pref("chat_recall_enabled", True)
    assert win.request_chat_index_update("save") is True
    ix = win._chat_indexer
    assert isinstance(ix, ChatIndexer)
    _wait_until(lambda: fake.n() == 1)
    assert win._ensure_chat_indexer() is ix


def test_save_session_requests_only_on_success(win, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    reasons = _record(monkeypatch, win)
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    win.set_session_saver(lambda: (["dsA"], []))
    win._save_session()
    assert reasons == ["save"]
    win.set_session_saver(lambda: ([], ["dsA"]))
    win._save_session()
    assert reasons == ["save"]


@pytest.mark.parametrize("res,expected", [
    ("restored:0", ["open"]), ("no-session:dsA", ["open"]),
    ("unreadable-session:dsA", ["open"]), ("error:dsA", []),
])
def test_open_dataset_requests_unless_error(win, monkeypatch, res, expected):
    from PySide6.QtWidgets import QMessageBox
    reasons = _record(monkeypatch, win)
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    win.register_command("open-dataset", lambda name: res)
    win._open_dataset_by_name("dsA")
    assert reasons == expected


def test_restore_last_session_requests_once(win, monkeypatch, tmp_path):
    from llm_bridge import paths as lb_paths
    reasons = _record(monkeypatch, win)
    lw = tmp_path / "last_window.json"
    lw.write_text(json.dumps({"version": 1, "datasets": ["dsA", "dsB"], "active": "dsA"}),
                  encoding="utf-8")
    monkeypatch.setattr(lb_paths, "last_window_path", lambda: lw)
    win.register_command("open-dataset", lambda name: "restored:0")
    win._restore_last_session()
    assert reasons == ["open"]


def test_stop_chat_indexer_drops_and_recreates(win):
    ix = win._ensure_chat_indexer()
    assert ix is not None
    win._stop_chat_indexer()
    assert win._chat_indexer is None and ix.request_update("x") is False
    win._stop_chat_indexer()
    assert win._ensure_chat_indexer() is not ix


class _FakeDialog:
    seen: ClassVar[dict] = {}

    def __init__(self, parent=None, *, indexer=None, on_changed=None):
        _FakeDialog.seen = {"indexer": indexer, "held": getattr(indexer, "_held", None)}
        self._on_changed = on_changed

    def exec(self):
        self._on_changed()
        return 0

    def deleteLater(self):
        _FakeDialog.seen["deleted"] = True


def test_open_embedding_settings_holds_and_releases(win, monkeypatch):
    from gui import embedding_settings_dialog as esd
    monkeypatch.setattr(esd, "EmbeddingSettingsDialog", _FakeDialog)
    reasons = _record(monkeypatch, win)
    win._open_embedding_settings()
    ix = win._chat_indexer
    assert _FakeDialog.seen == {"indexer": ix, "held": 1, "deleted": True}
    assert ix._held == 0 and reasons == ["settings"]


def test_open_embedding_settings_without_indexer(win, monkeypatch):
    from gui import embedding_settings_dialog as esd
    monkeypatch.setattr(esd, "EmbeddingSettingsDialog", _FakeDialog)

    def boom(self):
        raise RuntimeError("no indexer")
    monkeypatch.setattr(ChatIndexer, "__init__", boom)
    win._open_embedding_settings()
    assert _FakeDialog.seen["indexer"] is None and _FakeDialog.seen["deleted"]


def test_menu_item_and_retranslate(win):
    from common import i18n
    saved = (i18n._active, dict(i18n._catalogs))
    try:
        i18n._load_catalogs()
        assert win._embedding_settings_action in win._settings_menu.actions()
        i18n.set_language("en")
        win.retranslate()
        assert win._embedding_settings_action.text() \
            == i18n._catalogs["en"]["menu.settings.embeddings"]
        i18n.set_language("ja")
        win.retranslate()
        assert win._embedding_settings_action.text() \
            == i18n._catalogs["ja"]["menu.settings.embeddings"]
    finally:
        i18n._active, i18n._catalogs = saved[0], saved[1]
