"""GUI tests for chat search (Issue #108): dialog, run_search, links, highlight,
history panel, AI search turns, Ctrl+F action. Offscreen Qt."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from common.i18n import tr


class _FakeBackend:
    name = "mock"
    model = "fake-model"

    def set_persona(self, value):
        pass


@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def dirs(monkeypatch, tmp_path):
    import config
    mapping = {"dsA": tmp_path / "a", "dsB": tmp_path / "b"}
    for p in mapping.values():
        p.mkdir()
    monkeypatch.setattr(config, "get_dataset_dir", lambda name: mapping[name])
    return mapping


def _make_widget(monkeypatch, open_ds=("dsA", "dsB"), current="dsA"):
    import gui.chat as gchat
    import gui.chat_search as gcs
    from gui.chat import ChatWidget

    monkeypatch.setattr(gcs, "ai_search_engine", lambda: (None, "", ""))
    monkeypatch.setattr(gcs, "current_engine_id", lambda: "mock")
    monkeypatch.setattr(gcs, "current_model", lambda e: "")
    monkeypatch.setattr(gchat._StreamWorker, "start", lambda self: None)
    w = ChatWidget(_FakeBackend, dispatch=lambda *a, **k: None)
    monkeypatch.setattr(w, "_build_session_backend", lambda sess: _FakeBackend())
    win = MagicMock()
    win.open_dataset_names.return_value = list(open_ds)
    win.set_active_dataset.side_effect = lambda ds: (w.set_current_dataset(ds), True)[1]
    win.current_chat_dataset.return_value = None
    w.bind_window(win)
    w.set_current_dataset(current)
    return w, win


@pytest.fixture()
def env(qapp, monkeypatch, dirs):
    return _make_widget(monkeypatch)


def _add(widget, dataset, msgs, *, title="t", archived=False, updated=None):
    from llm_backend.base import Message
    from llm_bridge import chat_store
    s = chat_store.new_session("mock", "sys", dataset=dataset, title=title)
    for role, content in msgs:
        s.messages.append(Message(role=role, content=content))
    s.archived = archived
    if updated is not None:
        s.updated = updated
    widget._sessions.append(s)
    widget._rebuild_tab_bar()
    return s


def _req(query="peak", scope="dataset", dataset="dsA", archived=False, ai=False, hint=""):
    from llm_bridge.chat_search import SearchRequest
    return SearchRequest(query=query, scope=scope, dataset=dataset,
                         include_archived=archived, ai=ai, hint=hint)


def _status_texts(win):
    return [c.args[0] for c in win.statusBar.return_value.showMessage.call_args_list]


def _dialog(widget):
    from gui.chat_search import ChatSearchDialog
    return ChatSearchDialog(widget, None)


def _combo_texts(c):
    return [c.itemText(i) for i in range(c.count())]


def _set_mode(d, mode):
    d._mode_combo.setCurrentIndex(d._mode_items.index(mode))


def _is_ai(d):
    return d._mode() == "ai"


# ---- dialog ----


def test_dialog_scope_and_labels(env):
    w, _ = env
    d = _dialog(w)
    assert "dsA" in d._active_label.text() and not d._active_label.isHidden()
    assert _combo_texts(d._scope_combo) == [
        tr("chat.search.scope.dataset", dataset="dsA"),
        tr("chat.search.scope.dataset", dataset="dsB"),
        tr("chat.search.scope.all_named", datasets="dsA, dsB"),
    ]
    assert d._scope_combo.currentIndex() == 0
    assert d._switch_note.isHidden()
    d._query_edit.setText("x")
    d._scope_combo.setCurrentIndex(1)
    assert not d._switch_note.isHidden()
    assert d.request().dataset == "dsB" and d.request().scope == "dataset"
    assert d._query_edit.completer() is None and d._hint_edit.completer() is None


def test_dialog_no_dataset(qapp, monkeypatch, dirs):
    from PySide6.QtWidgets import QLabel
    from llm_bridge.paths import update_ui_pref
    update_ui_pref("chat_search_history_open", True)      # 開く設定でも履歴は出さない
    w, _ = _make_widget(monkeypatch, open_ds=(), current=None)
    d = _dialog(w)
    assert d._scope_combo.count() == 0
    assert d._active_label.text() == tr("chat.search.dialog.no_dataset")
    assert not d._active_label.isHidden()
    labels = [lb for lb in d.findChildren(QLabel) if lb.text() == tr("chat.search.dialog.scope")]
    assert len(labels) == 1 and labels[0].isHidden()
    assert d._scope_combo.isHidden() and d._switch_note.isHidden()
    assert d._history_toggle.isHidden() and d._history_panel.isHidden()
    d._query_edit.setText("x")
    r = d.request()
    assert r.scope == "unbound" and r.dataset is None


def test_dialog_search_button_and_ai_toggle(env):
    w, _ = env
    d = _dialog(w)
    _set_mode(d, "text")
    assert not d._search_btn.isEnabled()
    d._query_edit.setText("peak offset")
    assert d._search_btn.isEnabled()
    _set_mode(d, "ai")
    assert d._mode_stack.currentIndex() == 1
    assert d._ai_query_edit.toPlainText() == "peak offset"
    d._ai_query_edit.setPlainText("")
    assert not d._search_btn.isEnabled()
    d._ai_query_edit.setPlainText("line one\nline two")
    d._hint_edit.setText("h1 h2")
    assert d._search_btn.isEnabled()
    assert d.request().hint == "h1 h2" and d.request().ai
    _set_mode(d, "text")
    assert d._mode_stack.currentIndex() == 0
    assert d._query_edit.text() == "line one line two"
    assert d.request().hint == ""


def test_dialog_ctrl_enter_accepts(env):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QDialog
    w, _ = env
    d = _dialog(w)
    _set_mode(d, "ai")
    d._ai_query_edit.setPlainText("abc")
    d._ai_query_edit.moveCursor(d._ai_query_edit.textCursor().MoveOperation.End)
    QTest.keyClick(d._ai_query_edit, Qt.Key.Key_Return)
    assert d._ai_query_edit.toPlainText() == "abc\n"
    assert d.result() != QDialog.DialogCode.Accepted
    QTest.keyClick(d._ai_query_edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert d.result() == QDialog.DialogCode.Accepted


def test_dialog_ai_engine_label(env, monkeypatch):
    import gui.chat_search as gcs
    from llm_backend.engines import engine_by_id
    assert tr("chat.engine.mark_default") in gcs.ai_engine_label_text()
    monkeypatch.setattr(gcs, "ai_search_engine", lambda: (engine_by_id("codex"), "gpt-x", ""))
    text = gcs.ai_engine_label_text()
    assert tr("chat.engine.mark_override") in text and "gpt-x" in text
    w, _ = env
    assert _dialog(w)._engine_label.text() == text


def test_dialog_prefs_persist(env):
    from llm_bridge.paths import read_ui_pref
    w, _ = env
    d = _dialog(w)
    d._scope_combo.setCurrentIndex(2)
    d._archived_check.setChecked(True)
    _set_mode(d, "ai")
    d._ai_query_edit.setPlainText("q")
    d.accept()
    assert read_ui_pref("chat_search_scope", None) == "all"
    assert read_ui_pref("chat_search_archived", None) is True
    assert read_ui_pref("chat_search_ai", None) is True
    d2 = _dialog(w)
    assert d2._scope_combo.currentIndex() == 2
    assert d2._archived_check.isChecked() and _is_ai(d2)
    assert d2._mode_stack.currentIndex() == 1
    # dataset 選択は DS 名を保存しない → 次は前面 DS
    d2._scope_combo.setCurrentIndex(1)
    d2._ai_query_edit.setPlainText("q")
    d2.accept()
    d3 = _dialog(w)
    assert d3._scope_combo.currentIndex() == 0
    # キャンセルは保存しない
    d3._archived_check.setChecked(False)
    d3.reject()
    assert read_ui_pref("chat_search_archived", None) is True


# ---- run_search: standard ----


def test_run_search_standard(env):
    w, _ = env
    other = _add(w, "dsA", [("user", "peak offset fix"), ("assistant", "done peak")],
                 title="Other")
    emitted = []
    w.messageAdded.connect(lambda *a: emitted.append(a))
    n_before = len(w._sessions)
    w.run_search(_req())
    sess = w._active
    assert len(w._sessions) == n_before + 1
    assert sess.kind == "search"
    assert sess.title == tr("chat.search.title", query="peak")
    assert len(sess.messages) == 3
    md = sess.messages[2].content
    assert f"chatsearch:{other.id[:8]}:1?q=peak" in md
    assert f"chatsearch:{other.id[:8]}:2?q=peak" in md
    assert sess.search_spec == {"scope": "dataset", "dataset": "dsA",
                                "include_archived": False, "ai": False}
    assert len(emitted) == 2 and emitted[0][1] == "user" and emitted[1][1] == "assistant"
    html = w._log.document().toHtml()
    assert "chatsearch:" in html
    row = next(r for r in w.session_summaries() if r["id"] == sess.id)
    assert row["kind"] == "search"
    # 過去の検索セッションは hit に含まれない
    w.run_search(_req(query="peak"))
    md2 = w._active.messages[2].content
    assert sess.id[:8] not in md2
    assert md2.splitlines()[0] == _header("dsA", n=2)


def test_run_search_all_and_none(env):
    w, _ = env
    _add(w, "dsB", [("user", "peak b")], title="Btitle")
    w.run_search(_req(scope="all", dataset=None))
    md = w._active.messages[2].content
    assert "dsB / Btitle" in md
    w.run_search(_req(query="zzzqqq"))
    assert w._active.messages[2].content == _header("dsA", n=0)


def test_run_search_600_hits(env, dirs):
    w, _ = env
    _add(w, "dsA", [("user", f"hit {i}") for i in range(600)])
    w.run_search(_req(query="hit"))
    md = w._active.messages[2].content
    assert md.splitlines()[0] == _header("dsA", n=600)
    assert tr("chat.search.result.more", n=500) in md
    from llm_bridge import chat_search_history as h
    import dataset_config
    entries, _ = h.load_history(dataset_config.get_work_dir("dsA", create=False))
    assert entries[-1]["n_hits"] == 600


def test_run_search_unbound(qapp, monkeypatch, dirs):
    w, win = _make_widget(monkeypatch, open_ds=(), current=None)
    _add(w, None, [("user", "peak x")])
    w.run_search(_req(scope="unbound", dataset=None))
    assert w._active.dataset is None
    assert w._active.search_spec["scope"] == "unbound"
    win.set_active_dataset.assert_not_called()


def test_run_search_switches_dataset_first(env):
    w, win = env
    order = []
    orig = win.set_active_dataset.side_effect
    win.set_active_dataset.side_effect = lambda ds: (order.append(("switch", len(w._sessions))),
                                                     orig(ds))[1]
    n = len(w._sessions)
    w.run_search(_req(dataset="dsB"))
    assert order == [("switch", n)]
    assert w._active.dataset == "dsB" and w._current_dataset == "dsB"


def test_run_search_switch_failure(env):
    w, win = env
    win.set_active_dataset.side_effect = lambda ds: False
    n = len(w._sessions)
    w.run_search(_req(dataset="dsB"))
    assert len(w._sessions) == n
    assert tr("chat.search.dataset_closed", dataset="dsB") in _status_texts(win)


def test_run_search_all_without_front(env):
    w, win = env
    w.set_current_dataset(None)
    w.run_search(_req(scope="all", dataset=None))
    win.set_active_dataset.assert_called_with("dsA")
    assert w._active.dataset == "dsA"


# ---- history ----


def _history(ds):
    import dataset_config
    from llm_bridge import chat_search_history as h
    return h.load_history(dataset_config.get_work_dir(ds, create=False))


def _header(ds, *, n, target=None):
    """ds の最新の履歴の時刻で組んだ標準検索の見出し行（target 省略時は ds）。"""
    from llm_bridge.chat_search import format_ts
    entries, _ = _history(ds)
    return tr("chat.search.result.header", target=target or ds,
              time=format_ts(entries[-1]["ts"]), n=n)


def test_history_recorded_and_listed(env):
    w, _ = env
    _add(w, "dsA", [("user", "peak")])
    w.run_search(_req())
    std = w._active
    w.run_search(_req(query="find it", ai=True, hint="peak"))
    ai = w._active
    entries, status = _history("dsA")
    assert status == "ok"
    assert [e["session_id"] for e in entries] == [std.id, ai.id]
    assert entries[0]["n_hits"] == 1 and entries[1]["n_hits"] is None
    assert entries[1]["mode"] == "ai" and entries[1]["hint"] == "peak"
    d = _dialog(w)
    assert d._history_tree.topLevelItemCount() == 2
    assert d._history_tree.topLevelItem(0).text(2) == "find it"   # 新しい順
    assert d._history_tree.isColumnHidden(5)


def test_history_panel_collapsed_and_persisted(env):
    from llm_bridge.paths import read_ui_pref
    w, _ = env
    _add(w, "dsA", [("user", "peak")])
    w.run_search(_req())
    d = _dialog(w)
    assert not d._history_toggle.isChecked() and d._history_panel.isHidden()
    assert d._history_toggle.text() == tr("chat.search.history.title_n", n=1)
    assert read_ui_pref("chat_search_history_open", None) is None
    d._history_toggle.setChecked(True)
    assert read_ui_pref("chat_search_history_open", None) is True
    d2 = _dialog(w)
    assert d2._history_toggle.isChecked() and not d2._history_panel.isHidden()


def test_history_all_scope_merges(env):
    w, _ = env
    _add(w, "dsA", [("user", "peak")])
    _add(w, "dsB", [("user", "peak")])
    w.run_search(_req(dataset="dsA"))
    w.run_search(_req(dataset="dsB"))
    d = _dialog(w)
    d._scope_combo.setCurrentIndex(2)
    assert d._history_tree.topLevelItemCount() == 2
    assert not d._history_tree.isColumnHidden(5)
    assert {d._history_tree.topLevelItem(i).text(5) for i in range(2)} == {"dsA", "dsB"}


def test_history_select_reads_back(env):
    w, _ = env
    _add(w, "dsA", [("user", "peak")])
    w.run_search(_req(query="peak", archived=True))
    w.run_search(_req(query="ai question", ai=True, hint="hw"))
    w.set_current_dataset("dsA")
    d = _dialog(w)
    _set_mode(d, "text")
    d._archived_check.setChecked(False)
    tree = d._history_tree
    tree.setCurrentItem(tree.topLevelItem(1))    # 標準
    assert not _is_ai(d)
    assert d._query_edit.text() == "peak"
    assert d._archived_check.isChecked()
    assert tree.topLevelItemCount() == 2
    tree.setCurrentItem(tree.topLevelItem(0))    # AI
    assert _is_ai(d)
    assert d._ai_query_edit.toPlainText() == "ai question" and d._hint_edit.text() == "hw"
    assert tree.topLevelItemCount() == 2
    assert d._history_delete_btn.isEnabled()


def test_history_double_click_jumps(env, monkeypatch):
    import gui.chat_search as gcs
    from PySide6.QtWidgets import QDialog
    w, win = env
    _add(w, "dsB", [("user", "peak")])
    w.run_search(_req(dataset="dsB"))
    target = w._active
    w.set_current_dataset("dsA")
    win.set_active_dataset.reset_mock()
    # 対象を dsB にして一覧を出し、ダブルクリック
    monkeypatch.setattr(gcs, "load_scope_pref", lambda: "all")

    def fake_exec(self):
        item = self._history_tree.topLevelItem(0)
        self._history_tree.itemDoubleClicked.emit(item, 0)
        return self.result()

    monkeypatch.setattr(gcs.ChatSearchDialog, "exec", fake_exec)
    w.open_search()
    win.set_active_dataset.assert_called_with("dsB")
    assert w._active is target

    d = _dialog(w)
    d._history_tree.itemDoubleClicked.emit(d._history_tree.topLevelItem(0), 0)
    assert d.jump_session_id() == target.id and d.result() == QDialog.DialogCode.Accepted
    assert d.request() is None


def test_history_double_click_deleted_tab(env):
    from PySide6.QtWidgets import QDialog
    w, _ = env
    _add(w, "dsA", [("user", "peak")])
    w.run_search(_req())
    w._sessions.remove(w._active)
    w._active = w._sessions[0]
    d = _dialog(w)
    d._history_tree.itemDoubleClicked.emit(d._history_tree.topLevelItem(0), 0)
    assert d.result() != QDialog.DialogCode.Accepted
    assert d._history_note.text() == tr("chat.search.history.no_results_tab")


def test_history_delete(env):
    w, _ = env
    _add(w, "dsA", [("user", "peak")])
    w.run_search(_req())
    d = _dialog(w)
    d._history_tree.setCurrentItem(d._history_tree.topLevelItem(0))
    d._history_delete_btn.click()
    assert _history("dsA")[0] == []
    assert d._history_tree.topLevelItemCount() == 0


def test_history_mixed_valid_invalid(env, dirs):
    import dataset_config
    from llm_bridge import chat_search_history as h
    w, _ = env
    _add(w, "dsA", [("user", "peak")])
    w.run_search(_req())
    wd = dataset_config.get_work_dir("dsA", create=False)
    entries, _ = h.load_history(wd)
    bad = dict(entries[0])
    bad.pop("datasets")
    h.history_path(wd).write_text(json.dumps({"version": 1, "entries": [bad] + entries}),
                                  encoding="utf-8")
    (wd / "chat_search_history.json.bak").unlink()
    d = _dialog(w)
    assert d._history_tree.topLevelItemCount() == 1
    item = d._history_tree.topLevelItem(0)
    d._history_tree.setCurrentItem(item)
    d._history_tree.itemDoubleClicked.emit(item, 0)


def test_history_unconvertible_ts_dialog_opens(env, dirs):
    """同期で入った ts=1e20 / 巨大な整数の記録があってもダイアログが開き、正しい行だけ並ぶ。"""
    import dataset_config
    from llm_bridge import chat_search_history as h
    w, _ = env
    _add(w, "dsA", [("user", "peak")])
    w.run_search(_req())
    wd = dataset_config.get_work_dir("dsA", create=False)
    entries, _ = h.load_history(wd)
    bad1 = dict(entries[0], id="bad1", ts=1e20)
    bad2 = dict(entries[0], id="bad2", ts=10**400)
    h.history_path(wd).write_text(
        json.dumps({"version": 1, "entries": [bad1, bad2] + entries}), encoding="utf-8")
    (wd / "chat_search_history.json.bak").unlink()
    d = _dialog(w)
    assert d._history_tree.topLevelItemCount() == 1


def test_hits_markdown_unconvertible_updated():
    from gui import chat_search as gcs
    from llm_bridge.chat_search import SearchHit
    hit = SearchHit(session_id="abcdef12" + "0" * 24, dataset="dsA", title="t", archived=False,
                    msg_index=1, role="user", snippet="s", updated=1e20)
    md = gcs.hits_to_markdown([hit], _req(), datasets=["dsA"], when=0.0)
    assert " · - — " in md


def test_history_unreadable(env, dirs):
    import dataset_config
    from llm_bridge import chat_search_history as h
    w, win = env
    _add(w, "dsA", [("user", "peak")])
    wd = dataset_config.get_work_dir("dsA", create=True)
    p = h.history_path(wd)
    p.write_bytes(b"{broken")
    d = _dialog(w)
    assert d._history_note.text() == tr("chat.search.history.unreadable")
    w.run_search(_req())
    assert p.read_bytes() == b"{broken"
    assert tr("chat.search.history.write_failed") in _status_texts(win)


# ---- links ----


def _click(w, href):
    from PySide6.QtCore import QUrl
    w._on_anchor_clicked(QUrl(href))


def test_link_click_jumps_scrolls_highlights(env):
    w, _ = env
    target = _add(w, "dsA", [("user", "intro")] + [("assistant", "filler " * 50)] * 20
                  + [("user", "peak offset here")])
    w.run_search(_req(query="peak offset"))
    href = f"chatsearch:{target.id[:8]}:22?q=peak%20offset"
    assert href in w._active.messages[2].content
    _click(w, href)
    assert w._active is target
    assert w._log.textCursor().position() == w._msg_positions[22]
    assert w._log.extraSelections()
    assert w._last_active_by_ds["dsA"] == target.id
    # ズームで再描画しても残る
    w._zoom_in()
    assert w._log.extraSelections()
    # Esc で消える
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    QTest.keyClick(w._log, Qt.Key.Key_Escape)
    assert not w._log.extraSelections()


def test_link_highlight_cleared_on_switch(env):
    w, _ = env
    a = _add(w, "dsA", [("user", "peak a")])
    b = _add(w, "dsA", [("user", "other")])
    _click(w, f"chatsearch:{a.id[:8]}:1?q=peak")
    assert w._log.extraSelections()
    w.set_active_session_by_id(b.id)
    assert not w._log.extraSelections()


def test_link_other_dataset(env):
    w, win = env
    b = _add(w, "dsB", [("user", "peak b")])
    _click(w, f"chatsearch:{b.id[:8]}:1")
    win.set_active_dataset.assert_called_with("dsB")
    assert w._active is b
    assert not w._log.extraSelections()      # ?q 無し → スクロールのみ


def test_link_closed_dataset(env):
    w, win = env
    b = _add(w, "dsB", [("user", "peak b")])
    win.set_active_dataset.side_effect = lambda ds: False
    before = w._active
    _click(w, f"chatsearch:{b.id[:8]}:1")
    assert w._active is before
    assert tr("chat.search.dataset_closed", dataset="dsB") in _status_texts(win)


def test_link_archived_restores(env):
    w, _ = env
    a = _add(w, "dsA", [("user", "peak a")], archived=True)
    _click(w, f"chatsearch:{a.id[:8]}:1?q=peak")
    assert not a.archived and w._active is a


def test_link_unknown_and_invalid(env, monkeypatch):
    import gui.chat as gchat
    w, win = env
    _click(w, "chatsearch:ffffffff:1")
    assert tr("chat.search.link_missing") in _status_texts(win)
    opened = []
    monkeypatch.setattr(gchat.QDesktopServices, "openUrl", lambda u: opened.append(u))
    before = w._active
    _click(w, "chatsearch:xyz:1")
    assert opened == [] and w._active is before


# ---- turns ----


def test_start_turn_wire_text(env):
    w, _ = env
    sess = _add(w, "dsA", [])
    w.set_active_session_by_id(sess.id)
    w._start_turn(sess, "display", "local", wire_text="WIRE")
    assert sess.messages[-1].content == "display"
    assert w._turns[sess.id].worker._messages[-1].content == "WIRE"
    assert (len(sess.messages) - 1) in w._msg_positions


def _set_ai_engine(monkeypatch, value):
    import gui.chat_search as gcs
    monkeypatch.setattr(gcs, "ai_search_engine", lambda: value)


def test_run_search_ai(env, monkeypatch):
    from llm_backend.engines import engine_by_id
    w, _ = env
    _set_ai_engine(monkeypatch, (engine_by_id("mock"), "", ""))
    other = _add(w, "dsA", [("user", "peak offset")], title="OtherChat")
    w.run_search(_req(query="what did we conclude", ai=True))
    sess = w._active
    assert sess.kind == "search" and sess.engine == "mock"
    assert sess.messages[-1].content == tr("chat.search.ai_user_line",
                                           query="what did we conclude",
                                           scope="dsA")
    wire = w._turns[sess.id].worker._messages[-1].content
    for s in ("chat-list", "chat-search", "chat-show", "OtherChat", "what did we conclude",
              f"search_tab={sess.id[:8]}"):
        assert s in wire
    assert tr("chat.engine.mark_override") in w._log.toPlainText()
    assert other.id[:8] in wire


def test_run_search_ai_follow_global(env):
    w, _ = env
    _add(w, "dsA", [("user", "peak offset")])
    w.run_search(_req(query="q", ai=True))
    s = w._active
    assert (s.engine, s.engine_model, s.engine_provider) == (None, None, None)


def test_run_search_ai_hint(env):
    w, _ = env
    other = _add(w, "dsA", [("user", "peak offset")])
    w.run_search(_req(query="q", ai=True, hint="peak"))
    sess = w._active
    wire = w._turns[sess.id].worker._messages[-1].content
    assert "The user remembers these words: peak" in wire
    assert f"sid={other.id[:8]}" in wire
    assert sess.messages[-1].content == tr("chat.search.ai_user_line_hint", query="q",
                                           hint="peak", scope="dsA")


def test_run_search_ai_no_sessions(env):
    w, win = env
    n = len(w._sessions)
    w.run_search(_req(query="q", ai=True))
    assert len(w._sessions) == n
    assert tr("chat.search.no_sessions") in _status_texts(win)


def test_ai_done_renders_link(env):
    w, _ = env
    other = _add(w, "dsA", [("user", "peak offset")])
    w.run_search(_req(query="q", ai=True))
    sess = w._active
    w._turns[sess.id].buffer = f"- [x › #1](chatsearch:{other.id[:8]}:1) — why"
    w._on_done(sess.id)
    assert "chatsearch:" in w._log.document().toHtml()


def _finish(w, sess):
    w._turns[sess.id].buffer = "answer"
    w._on_done(sess.id)


def test_ai_followup_gets_instructions(env):
    from llm_bridge import chat_store
    w, _ = env
    _add(w, "dsA", [("user", "peak offset")])
    w.run_search(_req(query="q", ai=True))
    sess = w._active
    _finish(w, sess)
    w._input.setPlainText("what value then?")
    w._on_send()
    assert sess.messages[-1].content == "what value then?"
    wire = w._turns[sess.id].worker._messages[-1].content
    assert "Follow-up question from the user:" in wire
    assert "what value then?" in wire
    assert f"search_tab={sess.id[:8]}" in wire
    for s in ("chat-list", "chat-search", "chat-show"):
        assert s in wire
    assert "<chat_index>" not in wire
    _finish(w, sess)
    # 再起動・別 PC 相当: 保存形式を往復したセッションでも付く
    fresh = chat_store.session_from_dict(chat_store.session_to_dict(sess))
    w._sessions[w._sessions.index(sess)] = fresh
    w._active = fresh
    w._input.setPlainText("again?")
    w._on_send()
    wire2 = w._turns[fresh.id].worker._messages[-1].content
    assert "Follow-up question from the user:" in wire2 and "again?" in wire2


def test_plain_and_standard_tabs_send_raw_text(env):
    w, _ = env
    plain = _add(w, "dsA", [])
    w.set_active_session_by_id(plain.id)
    w._input.setPlainText("hello")
    w._on_send()
    assert w._turns[plain.id].worker._messages[-1].content == "hello"
    _add(w, "dsA", [("user", "peak")])
    w.run_search(_req())
    std = w._active
    w._input.setPlainText("more")
    w._on_send()
    assert w._turns[std.id].worker._messages[-1].content == "more"


def test_fork_of_ai_search_tab_is_chat(env):
    w, _ = env
    _add(w, "dsA", [("user", "peak offset")])
    w.run_search(_req(query="q", ai=True))
    sess = w._active
    _finish(w, sess)
    w._fork_from(sess, cut=len(sess.messages), prefill=None)
    fork = w._active
    assert fork is not sess and fork.kind == "chat"
    w._input.setPlainText("next")
    w._on_send()
    assert w._turns[fork.id].worker._messages[-1].content == "next"


def test_ai_tab_with_unsafe_id_sends_raw(env):
    from llm_bridge import chat_store
    w, _ = env
    s = chat_store.new_session("mock", "sys", dataset="dsA", kind="search",
                               search_spec={"scope": "dataset", "dataset": "dsA",
                                            "include_archived": False, "ai": True})
    d = chat_store.session_to_dict(s)
    d["id"] = "x;rm -rf"
    bad = chat_store.session_from_dict(d)
    w._sessions.append(bad)
    w._rebuild_tab_bar()
    w.set_active_session_by_id(bad.id)
    w._input.setPlainText("question")
    w._on_send()
    assert w._turns[bad.id].worker._messages[-1].content == "question"


# ---- window action ----


def test_window_ctrl_f_action(qapp, monkeypatch):
    import common.i18n as i18n
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeySequence
    from gui.chat import ChatWidget
    from gui.window import ToolWindow

    i18n._load_catalogs()
    win = ToolWindow()
    cw = ChatWidget(_FakeBackend, dispatch=lambda *a, **k: None)
    win.set_chat_widget(cw)
    act = win._chat_search_action
    assert act.shortcut() == QKeySequence(QKeySequence.StandardKey.Find)
    assert act.shortcutContext() == Qt.ShortcutContext.ApplicationShortcut
    assert act in win._view_menu.actions()
    saved = i18n.current_language()
    try:
        i18n.set_language("en")
        win.retranslate()
        en = act.text()
        i18n.set_language("ja")
        win.retranslate()
        assert act.text() != en
    finally:
        i18n.set_language(saved)
    called = []
    monkeypatch.setattr(cw, "open_search", lambda: called.append(True))
    act.trigger()
    assert called == [True]


# ---- Issue #110: 対象の選択を itemData に頼らない / 結果の見出し ----


@pytest.fixture()
def list_item_data(monkeypatch):
    """PySide6 6.10.1 の挙動を再現する: itemData / currentData の tuple が list で返る。"""
    import gui.chat_search as gcs
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QComboBox

    def _as_list(d):
        return list(d) if isinstance(d, tuple) else d

    class _ListDataCombo(QComboBox):
        def currentData(self, role=Qt.ItemDataRole.UserRole):
            return _as_list(super().currentData(role))

        def itemData(self, index, role=Qt.ItemDataRole.UserRole):
            return _as_list(super().itemData(index, role))

    monkeypatch.setattr(gcs, "QComboBox", _ListDataCombo)


def test_dialog_every_scope_item(env, list_item_data):
    w, _ = env
    d = _dialog(w)
    d._query_edit.setText("x")
    got = []
    for i in range(d._scope_combo.count()):
        d._scope_combo.setCurrentIndex(i)
        r = d.request()
        got.append((r.scope, r.dataset))
    assert got == [("dataset", "dsA"), ("dataset", "dsB"), ("all", None)]
    d._scope_combo.setCurrentIndex(1)
    assert not d._switch_note.isHidden()
    assert all(d._scope_combo.itemData(i) is None for i in range(d._scope_combo.count()))


def test_history_readback_sets_scope(env, list_item_data, monkeypatch):
    import gui.chat_search as gcs
    w, _ = env
    _add(w, "dsB", [("user", "peak")])
    w.run_search(_req(dataset="dsB"))
    w.run_search(_req(query="peak all", scope="all", dataset=None))
    w.set_current_dataset("dsA")
    monkeypatch.setattr(gcs, "load_scope_pref", lambda: "all")
    d = _dialog(w)
    tree = d._history_tree
    assert tree.topLevelItemCount() == 2
    row = {e["scope"]: i for i, (_, e) in enumerate(d._history_rows)}
    tree.setCurrentItem(tree.topLevelItem(row["dataset"]))
    assert d._scope_combo.currentIndex() == 1          # データセット: dsB
    tree.setCurrentItem(tree.topLevelItem(row["all"]))
    assert d._scope_combo.currentIndex() == 2          # 開いている全データセット


def test_search_other_dataset_real_window(qapp, monkeypatch, dirs, list_item_data):
    """前面 dsA でダイアログの対象を dsB にして検索 → dsB が前面になり、結果は dsB の一覧に入る。"""
    from gui.chat import ChatWidget
    from gui.window import ToolWindow
    win = ToolWindow()
    cw = ChatWidget(_FakeBackend, dispatch=lambda *a, **k: None)
    win.set_chat_widget(cw)
    win._ensure_group("dsA")
    win._ensure_group("dsB")
    win.note_current_dataset("dsA")
    _add(cw, "dsA", [("user", "peak in A")])
    _add(cw, "dsB", [("user", "peak in B")])
    d = _dialog(cw)
    d._query_edit.setText("peak")
    d._scope_combo.setCurrentIndex(1)                  # データセット: dsB
    cw.run_search(d.request())
    sess = cw._active
    assert sess.kind == "search" and sess.dataset == "dsB"
    assert win.current_dataset == "dsB" and cw._current_dataset == "dsB"

    def tab_ids():
        return [cw._tab_bar.tabData(i) for i in range(cw._tab_bar.count())]

    assert sess.id in tab_ids()
    entries, status = _history("dsB")
    assert status == "ok" and [e["session_id"] for e in entries] == [sess.id]
    assert _history("dsA") == ([], "absent")
    assert sess.messages[1].content == tr("chat.search.user_line", query="peak")
    assert sess.messages[2].content.splitlines()[0] == _header("dsB", n=1)
    win.set_active_dataset("dsA")
    assert sess.id not in tab_ids()


def _hit(ds="dsB", title="t"):
    from llm_bridge.chat_search import SearchHit
    return SearchHit(session_id="abcdef12" + "0" * 24, dataset=ds, title=title, archived=False,
                     msg_index=1, role="user", snippet="s", updated=0.0)


def test_hits_markdown_header():
    from gui import chat_search as gcs
    from llm_bridge.chat_search import format_ts
    when = 1_790_000_000.0
    t = format_ts(when)
    md = gcs.hits_to_markdown([_hit()], _req(scope="all", dataset=None),
                              datasets=["dsA", "dsB"], when=when)
    assert md.splitlines()[0] == tr("chat.search.result.header", target="dsA, dsB", time=t, n=1)
    assert "dsB / t" in md
    md = gcs.hits_to_markdown([_hit()], _req(dataset="dsB", archived=True),
                              datasets=["dsB"], when=when)
    assert md.splitlines()[0] == (tr("chat.search.result.header", target="dsB", time=t, n=1)
                                  + tr("chat.search.result.archived_included"))
    md = gcs.hits_to_markdown([], _req(dataset="ds_x"), datasets=["ds_x"], when=when)
    assert md == tr("chat.search.result.header", target="ds\\_x", time=t, n=0)
    md = gcs.hits_to_markdown([], _req(scope="unbound", dataset=None), datasets=[], when=when)
    assert md == tr("chat.search.result.header_unbound", time=t, n=0)


def test_run_search_ai_display_line_all(env):
    w, _ = env
    _add(w, "dsA", [("user", "peak offset")])
    w.run_search(_req(query="q", scope="all", dataset=None, ai=True))
    assert w._active.messages[-1].content == tr("chat.search.ai_user_line", query="q",
                                                scope="dsA, dsB")


def test_run_search_ai_display_line_unbound(qapp, monkeypatch, dirs):
    w, _ = _make_widget(monkeypatch, open_ds=(), current=None)
    _add(w, None, [("user", "peak x")])
    w.run_search(_req(query="q", scope="unbound", dataset=None, ai=True))
    assert w._active.messages[-1].content == tr("chat.search.ai_user_line_unbound", query="q")
    w.run_search(_req(query="q2", scope="unbound", dataset=None, ai=True, hint="peak"))
    assert w._active.messages[-1].content == tr("chat.search.ai_user_line_hint_unbound",
                                                query="q2", hint="peak")


def test_removed_search_keys_absent():
    import tomllib
    from common.paths import repo_root
    for lang in ("ja", "en"):
        with open(repo_root() / "i18n" / f"{lang}.toml", "rb") as f:
            cat = tomllib.load(f)
        for key in ("chat.search.scope.none", "chat.search.result.count",
                    "chat.search.result.none"):
            assert key not in cat, (lang, key)


def test_run_search_all_window_unknown(env):
    """open_datasets が分からない（window 不明）と core は DS を絞らない → 見出し・履歴も検索した DS。"""
    w, win = env
    _add(w, "dsA", [("user", "peak a")])
    _add(w, "dsC", [("user", "peak c")])           # 開いている一覧に無い DS も検索される
    win.open_dataset_names.return_value = None
    w.run_search(_req(scope="all", dataset=None))
    md = w._active.messages[2].content
    entries, _ = _history("dsA")
    assert entries[-1]["datasets"] == ["dsA", "dsC"]
    assert md.splitlines()[0] == _header("dsA", n=2, target="dsA, dsC")


def test_run_search_unbound_chat_in_range(env):
    """未所属のチャットは dataset / all のどちらの範囲でも検索に入る。対象には書かず、hit 行に DS 名は付かない。"""
    w, _ = env
    loose = _add(w, None, [("user", "peak loose")], title="Loose")
    a = _add(w, "dsA", [("user", "peak a")], title="Atitle")
    w.run_search(_req(dataset="dsA"))
    md = w._active.messages[2].content
    assert md.splitlines()[0] == _header("dsA", n=2)
    assert f"[Loose](chatsearch:{loose.id[:8]}:1?q=peak)" in md
    w.run_search(_req(scope="all", dataset=None))
    md = w._active.messages[2].content
    assert md.splitlines()[0] == _header("dsA", n=2, target="dsA, dsB")
    assert f"[Loose](chatsearch:{loose.id[:8]}:1?q=peak)" in md
    assert f"[dsA / Atitle](chatsearch:{a.id[:8]}:1?q=peak)" in md


def test_run_search_all_window_unknown_bad_dataset(env):
    """同期ファイル由来の str 以外の dataset が混ざっても検索は落ちず、見出し・履歴には str だけが出る。"""
    w, win = env
    _add(w, "dsA", [("user", "peak a")])
    for bad in (3, ["x"]):                         # session_from_dict は dataset の型を検査しない
        s = _add(w, "dsA", [("user", "other")])
        s.dataset = bad
    win.open_dataset_names.return_value = None
    w.run_search(_req(scope="all", dataset=None))
    entries, _ = _history("dsA")
    assert entries[-1]["datasets"] == ["dsA"]
    assert w._active.messages[2].content.splitlines()[0] == _header("dsA", n=1)


# ---- エンジン未設定（Issue #115） ----


def test_run_search_ai_unconfigured_refuses(env):
    from llm_backend.unconfigured import UnconfiguredBackend

    w, win = env
    _add(w, "dsA", [("user", "peak offset")])
    w._backend = UnconfiguredBackend()
    n = len(w._sessions)
    w.run_search(_req(query="q", ai=True))
    assert len(w._sessions) == n
    assert w._turns == {}
    assert tr("chat.engine.unconfigured") in _status_texts(win)
    assert _history("dsA")[0] == []
    w.run_search(_req())        # 標準検索は未設定でも検索タブを作る
    assert len(w._sessions) == n + 1


def test_dialog_ai_engine_label_unconfigured(env, monkeypatch):
    import gui.chat_search as gcs

    monkeypatch.setattr(gcs, "current_engine_id", lambda: None)
    assert gcs.ai_engine_label_text() == tr(
        "chat.search.dialog.ai_engine", engine=tr("chat.engine.none"), model="-",
        mark=tr("chat.engine.mark_default"))



# ---- 過去チャットの関連検索（Issue #127） ----


class _FakeIndex:
    def __init__(self, hits=(), error=None):
        self.hits = list(hits)
        self.error = error
        self.calls: list = []

    def search(self, query, *, datasets=None, include_archived=False, k=20):
        self.calls.append((query, datasets, include_archived, k))
        if self.error is not None:
            raise self.error
        return list(self.hits), []


def _recall_hit(sid, idx=1, dataset="dsA", snippet="offset was 0.12"):
    from llm_bridge.chat_index import RecallHit
    return RecallHit(sid=sid, sid8=sid[:8], idx=idx, dataset=dataset, title="Peak chat",
                     archived=False, role="assistant", score=0.03, cosine=0.8, lexical=0.0,
                     snippet=snippet, updated=0.0)


def _recall_env(monkeypatch, *, ready=True, in_ai=True, index=None):
    """関連検索を使える状態にする（pref・is_ready・load_prefs・ChatIndex.open を差し替える）。"""
    from llm_bridge import chat_index
    from llm_bridge.paths import update_ui_pref
    update_ui_pref("chat_recall_enabled", True)
    ready_fn = ready if callable(ready) else (lambda: ready)
    monkeypatch.setattr(chat_index, "is_ready", ready_fn)
    monkeypatch.setattr(chat_index, "load_prefs", lambda: chat_index.RecallPrefs(
        True, "ruri-v3-30m", in_ai))
    idx = index if index is not None else _FakeIndex()
    opened = []

    def _open(embedder=None):
        opened.append(True)
        return idx

    monkeypatch.setattr(chat_index.ChatIndex, "open", staticmethod(_open))

    def _no_status(*a, **k):
        raise AssertionError("status() must not be called on the GUI thread")

    monkeypatch.setattr(chat_index, "status", _no_status)
    return idx, opened


class _RecallBackend:
    name = "mock"
    model = "fake-model"

    def __init__(self):
        self.recall_calls: list = []

    def set_persona(self, value):
        pass

    def set_chat_recall(self, value):
        self.recall_calls.append(value)


def _wire_and_tools(w):
    sess = w._active
    worker = w._turns[sess.id].worker
    return sess, worker._messages[-1].content, [t["function"]["name"] for t in worker._tools]


def test_run_search_ai_not_ready_is_byte_identical(env, monkeypatch):
    """使えない状態（既定）では、wire は新しい引数なしの build_ai_prompt と同じ。"""
    from llm_bridge import chat_index
    from llm_bridge import chat_search as core
    w, win = env
    _add(w, "dsA", [("user", "peak offset")], title="OtherChat")
    calls = []
    orig = core.build_ai_prompt

    def spy(*a, **k):
        calls.append((a, dict(k)))
        return orig(*a, **k)

    monkeypatch.setattr(core, "build_ai_prompt", spy)
    monkeypatch.setattr(chat_index.ChatIndex, "open", staticmethod(
        lambda embedder=None: (_ for _ in ()).throw(AssertionError("must not open"))))
    w.run_search(_req(query="what did we conclude", ai=True, hint="peak"))
    (args, kw), = calls
    assert kw.pop("related_hits") == [] and kw.pop("recall_args") is None
    _sess, wire, tools = _wire_and_tools(w)
    assert wire == orig(*args, **kw)
    assert "chat-recall" not in wire and "related_hits" not in wire
    assert "chat_recall" not in tools
    assert tr("chat.search.recall_skipped") not in _status_texts(win)


@pytest.mark.parametrize("hint", ["", "peak"])
def test_run_search_ai_with_related_hits(env, monkeypatch, hint):
    w, _win = env
    other = _add(w, "dsA", [("user", "peak offset")], title="OtherChat")
    idx, _ = _recall_env(monkeypatch, index=_FakeIndex([_recall_hit(other.id)]))
    monkeypatch.setattr(w, "_build_session_backend", lambda sess: _RecallBackend())
    w.run_search(_req(query="what offset", ai=True, hint=hint))
    sess, wire, tools = _wire_and_tools(w)
    assert idx.calls == [("what offset", ["dsA"], False, 30)]
    assert f"\n<related_hits>\n- sid={other.id[:8]} idx=1 | ds=dsA | Peak chat" in wire
    assert 'chat-recall "<question or keywords>" [-k 20]' in wire
    assert "--dataset" not in wire
    assert ("<keyword_hits>\n" in wire) == bool(hint)
    assert tools[-1] == "chat_recall"
    assert w._session_backends[sess.id].recall_calls == [True]


def test_run_search_ai_related_scope_all(env, monkeypatch):
    w, _ = env
    _add(w, "dsA", [("user", "peak offset")])
    idx, _ = _recall_env(monkeypatch)
    w.run_search(_req(query="q", scope="all", dataset=None, ai=True, archived=True))
    _sess, wire, _tools = _wire_and_tools(w)
    assert idx.calls == [("q", ["dsA", "dsB"], True, 30)]
    assert 'chat-recall "<question or keywords>" --all --archived [-k 20]' in wire
    assert "related_hits" not in wire           # 0 件ならブロックは付けない


def test_run_search_ai_pref_off_keeps_tool_line(env, monkeypatch):
    w, win = env
    _add(w, "dsA", [("user", "peak offset")])
    _idx, opened = _recall_env(monkeypatch, in_ai=False)
    w.run_search(_req(query="q", ai=True))
    _sess, wire, tools = _wire_and_tools(w)
    assert "related_hits" not in wire and opened == []
    assert "chat-recall" in wire and tools[-1] == "chat_recall"
    assert tr("chat.search.recall_skipped") not in _status_texts(win)


def test_run_search_ai_related_failure_is_reported(env, monkeypatch):
    w, win = env
    _add(w, "dsA", [("user", "peak offset")])
    _recall_env(monkeypatch, index=_FakeIndex(error=RuntimeError("onnx broke")))
    w.run_search(_req(query="q", ai=True))
    sess, wire, _tools = _wire_and_tools(w)
    assert sess.id in w._turns                  # ターンは始まる
    assert "related_hits" not in wire and "chat-recall" in wire
    assert tr("chat.search.recall_skipped") in _status_texts(win)


def test_run_search_ai_unbound_has_no_recall(qapp, monkeypatch, dirs):
    w, _ = _make_widget(monkeypatch, open_ds=(), current=None)
    _add(w, None, [("user", "peak x")])
    _idx, opened = _recall_env(monkeypatch)
    w.run_search(_req(query="q", scope="unbound", dataset=None, ai=True))
    _sess, wire, _tools = _wire_and_tools(w)
    assert "chat-recall" not in wire and "related_hits" not in wire and opened == []


def test_run_search_ai_uses_one_readiness_for_the_turn(env, monkeypatch):
    """初回の判定を wire・system の規則・ツールで共有する（途中で状態が変わっても揃う）。"""
    w, _ = env
    _add(w, "dsA", [("user", "peak offset")])
    answers = iter([True])
    _recall_env(monkeypatch, ready=lambda: next(answers, False))
    monkeypatch.setattr(w, "_build_session_backend", lambda sess: _RecallBackend())
    w.run_search(_req(query="q", ai=True))
    sess, wire, tools = _wire_and_tools(w)
    assert "chat-recall" in wire and tools[-1] == "chat_recall"
    assert w._session_backends[sess.id].recall_calls == [True]


@pytest.mark.parametrize("ready", [True, False])
def test_ai_followup_recall_line_follows_readiness(env, monkeypatch, ready):
    w, _ = env
    _add(w, "dsA", [("user", "peak offset")])
    w.run_search(_req(query="q", ai=True))
    sess = w._active
    _finish(w, sess)
    _recall_env(monkeypatch, ready=ready)
    w._input.setPlainText("and then?")
    w._on_send()
    _sess, wire, tools = _wire_and_tools(w)
    assert "Follow-up question from the user:" in wire
    assert ("chat-recall" in wire) is ready
    assert ("chat_recall" in tools) is ready
    assert "related_hits" not in wire


def test_related_hits_for_ai_conditions(monkeypatch):
    import gui.chat_search as gcs
    hit = _recall_hit("0123456789abcdef0123456789abcdef")
    idx, _ = _recall_env(monkeypatch, index=_FakeIndex([hit]))
    assert gcs.related_hits_for_ai("q", ["dsA"], False) == [hit]
    assert idx.calls == [("q", ["dsA"], False, 30)]
    assert gcs.related_hits_for_ai("q", [], False) == []            # 範囲が空
    _recall_env(monkeypatch, in_ai=False)
    assert gcs.related_hits_for_ai("q", ["dsA"], False) == []       # AI 検索に渡さない設定
    _recall_env(monkeypatch, ready=False)
    assert gcs.related_hits_for_ai("q", ["dsA"], False) == []       # 使えない
    _recall_env(monkeypatch, index=_FakeIndex(error=RuntimeError("x")))
    assert gcs.related_hits_for_ai("q", ["dsA"], False) is None     # 失敗（送出しない）


# ---- Ctrl+F の 4 モード（Issue #128）: ダイアログ・Markdown ----


def _items(d):
    return list(d._scope_items)


def test_dialog_off_is_two_choices_like_before(env, monkeypatch):
    from llm_bridge import chat_index
    from llm_bridge.paths import read_ui_pref, update_ui_pref

    def boom(*a, **k):
        raise AssertionError("the index module must not be consulted while the feature is off")

    monkeypatch.setattr(chat_index, "readiness", boom)
    monkeypatch.setattr(chat_index, "is_ready", boom)
    update_ui_pref("chat_search_mode", "semantic")   # 使える状態のときに保存された値
    update_ui_pref("chat_search_scope", "registered")
    w, _ = env
    d = _dialog(w)
    assert d._mode_items == ["text", "ai"]
    assert _combo_texts(d._mode_combo) == [tr("chat.search.mode.text"), tr("chat.search.mode.ai")]
    assert d._mode() == "text"                       # 一致検索に倒す
    assert _items(d) == [("dataset", "dsA"), ("dataset", "dsB"), ("all", None)]
    assert d._scope_combo.currentIndex() == 0        # "registered" は前面の DS（旧ビルドと同じ）
    assert d._form.isRowVisible(d._engine_label) is False
    assert d._engine_label.text()                    # 文言は常にある
    assert d._form.isRowVisible(d._embed_label) is False
    _set_mode(d, "ai")
    assert d._form.isRowVisible(d._engine_label) and not d._form.isRowVisible(d._embed_label)
    d._ai_query_edit.setPlainText("q")
    d.accept()
    assert read_ui_pref("chat_search_ai", None) is True
    assert read_ui_pref("chat_search_mode", None) == "semantic"   # オフでは書かない
    assert read_ui_pref("chat_search_scope", None) == "dataset"


def test_dialog_ready_has_four_modes_and_registered_scope(env, monkeypatch):
    _recall_env(monkeypatch)
    w, _ = env
    d = _dialog(w)
    assert d._mode_items == ["text", "semantic", "hybrid", "ai"]
    assert d._mode() == "text"
    base = [("dataset", "dsA"), ("dataset", "dsB"), ("all", None)]
    assert _items(d) == base
    d._scope_combo.setCurrentIndex(2)                       # 開いている全データセット
    _set_mode(d, "semantic")
    assert _items(d) == [*base, ("registered", None)]
    assert d._scope_data() == ("all", None)                 # 選択は保つ
    assert _combo_texts(d._scope_combo)[-1] == tr("chat.search.scope.registered")
    assert d._form.isRowVisible(d._embed_label) and not d._form.isRowVisible(d._engine_label)
    assert "ruri" in d._embed_label.text().casefold()
    d._scope_combo.setCurrentIndex(3)
    assert d._switch_note.isHidden()                        # 埋め込み検索は切替も注記もなし
    _set_mode(d, "hybrid")
    assert d._scope_data() == ("registered", None)
    assert d._switch_note.text() == tr("chat.search.scope.registered_note")
    d._query_edit.setText("peak")
    r = d.request()
    assert (r.mode, r.scope, r.dataset, r.ai) == ("hybrid", "registered", None, False)
    _set_mode(d, "text")                                    # 一致検索に戻すと全データセット
    assert _items(d) == base and d._scope_data() == ("all", None)
    _set_mode(d, "ai")
    assert d._form.isRowVisible(d._engine_label) and d._form.isRowVisible(d._embed_label)
    assert d._ai_query_edit.toPlainText() == "peak"


def test_dialog_ready_ai_hides_embed_label_when_not_passed(env, monkeypatch):
    _recall_env(monkeypatch, in_ai=False)
    w, _ = env
    d = _dialog(w)
    _set_mode(d, "ai")
    assert not d._form.isRowVisible(d._embed_label)
    _set_mode(d, "semantic")
    assert d._form.isRowVisible(d._embed_label)


def test_dialog_ready_without_dataset_offers_registered(qapp, monkeypatch, dirs):
    _recall_env(monkeypatch)
    w, _ = _make_widget(monkeypatch, open_ds=(), current=None)
    d = _dialog(w)
    assert _items(d) == [] and not d._form.isRowVisible(d._scope_combo)
    _set_mode(d, "semantic")
    assert _items(d) == [("registered", None)]
    assert d._form.isRowVisible(d._scope_combo) and not d._scope_combo.isHidden()
    assert d._switch_note.isHidden()
    assert d._history_toggle.isHidden() and d._history_panel.isHidden()
    d._query_edit.setText("x")
    assert d.request().scope == "registered"
    _set_mode(d, "text")
    assert _items(d) == [] and not d._form.isRowVisible(d._scope_combo)
    assert d.request().scope == "unbound"


def test_dialog_mode_prefs_persist(env, monkeypatch):
    from llm_bridge.paths import read_ui_pref, update_ui_pref
    _recall_env(monkeypatch)
    w, _ = env
    d = _dialog(w)
    _set_mode(d, "hybrid")
    d._scope_combo.setCurrentIndex(3)
    d._query_edit.setText("q")
    d.accept()
    assert read_ui_pref("chat_search_mode", None) == "hybrid"
    assert read_ui_pref("chat_search_ai", None) is False
    assert read_ui_pref("chat_search_scope", None) == "registered"
    d2 = _dialog(w)
    assert d2._mode() == "hybrid" and d2._scope_data() == ("registered", None)
    # 旧ビルドが AI 検索に変えた（chat_search_ai だけ書いた）→ AI 検索
    update_ui_pref("chat_search_ai", True)
    assert _dialog(w)._mode() == "ai"
    # mode は "ai" なのに ai が偽（旧ビルドが一致検索に戻した）→ 一致検索
    update_ui_pref("chat_search_mode", "ai")
    update_ui_pref("chat_search_ai", False)
    assert _dialog(w)._mode() == "text"
    update_ui_pref("chat_search_mode", 3)
    assert _dialog(w)._mode() == "text"


def test_dialog_mode_tooltips(env, monkeypatch):
    from PySide6.QtCore import Qt
    _recall_env(monkeypatch)
    w, _ = env
    d = _dialog(w)
    tips = [d._mode_combo.itemData(i, Qt.ItemDataRole.ToolTipRole)
            for i in range(d._mode_combo.count())]
    assert tips == [None, tr("chat.search.dialog.semantic_hint"),
                    tr("chat.search.dialog.hybrid_hint"), tr("chat.search.dialog.ai_hint")]
    _set_mode(d, "ai")
    assert d._mode_combo.toolTip() == tr("chat.search.dialog.ai_hint")


def test_dialog_every_mode_and_scope_item(env, monkeypatch, list_item_data):
    _recall_env(monkeypatch)
    w, _ = env
    d = _dialog(w)
    got = []
    for m in d._mode_items:
        _set_mode(d, m)
        d._query_edit.setText("x")
        d._ai_query_edit.setPlainText("x")
        for i in range(d._scope_combo.count()):
            d._scope_combo.setCurrentIndex(i)
            r = d.request()
            got.append((r.mode, r.scope, r.dataset))
    assert ("semantic", "registered", None) in got and ("hybrid", "registered", None) in got
    assert not any(s == "registered" for m, s, _ in got if m in ("text", "ai"))
    assert all(d._mode_combo.itemData(i) is None for i in range(d._mode_combo.count()))


def _history_entry(ds, req, *, n_hits=3, datasets=None):
    import dataset_config
    from llm_bridge import chat_search_history as h
    wd = dataset_config.get_work_dir(ds, create=True)
    e = h.new_entry(req, datasets=[ds] if datasets is None else datasets, n_hits=n_hits,
                    session_id="0123456789abcdef0123456789abcdef")
    assert h.append_history(wd, e)
    return e


def test_history_embedding_rows_listed_and_read_back(env, monkeypatch):
    from llm_bridge.chat_search import SearchRequest
    _recall_env(monkeypatch)
    w, _ = env
    _history_entry("dsA", SearchRequest(query="sem q", scope="dataset", dataset="dsA",
                                        include_archived=False, ai=False, mode="semantic"))
    _history_entry("dsA", SearchRequest(query="hyb q", scope="registered", dataset=None,
                                        include_archived=True, ai=False, mode="hybrid"),
                   datasets=[])
    d = _dialog(w)
    tree = d._history_tree
    assert tree.topLevelItemCount() == 2
    rows = {tree.topLevelItem(i).text(2): tree.topLevelItem(i) for i in range(2)}
    assert rows["sem q"].text(1) == tr("chat.search.history.mode_semantic")
    assert rows["hyb q"].text(1) == tr("chat.search.history.mode_hybrid")
    assert rows["hyb q"].text(3) == tr("chat.search.scope.registered_short")
    tree.setCurrentItem(rows["hyb q"])
    assert d._mode() == "hybrid" and d._scope_data() == ("registered", None)
    assert d._query_edit.text() == "hyb q" and d._archived_check.isChecked()
    assert tree.topLevelItemCount() == 2               # 読み戻しで一覧を作り直さない
    tree.setCurrentItem(rows["sem q"])
    assert d._mode() == "semantic" and d._scope_data() == ("dataset", "dsA")


def test_history_embedding_rows_when_off(env):
    from llm_bridge.chat_search import SearchRequest
    w, _ = env
    _history_entry("dsA", SearchRequest(query="hyb q", scope="registered", dataset=None,
                                        include_archived=False, ai=False, mode="hybrid"),
                   datasets=[])
    d = _dialog(w)
    item = d._history_tree.topLevelItem(0)
    assert item.text(1) == tr("chat.search.history.mode_hybrid")
    d._history_tree.setCurrentItem(item)
    assert d._mode() == "text" and d._scope_data() == ("all", None)
    assert d._query_edit.text() == "hyb q"


def test_hits_markdown_full_text():
    """括り出した行の生成が、一致検索の結果の全文を変えないこと。"""
    from gui import chat_search as gcs
    from llm_bridge.chat_search import SearchHit, format_ts
    hits = [SearchHit(session_id="abcdef12" + "0" * 24, dataset="dsB", title="t_1",
                      archived=True, msg_index=3, role="user", snippet="a*b", updated=0.0),
            SearchHit(session_id="12345678" + "0" * 24, dataset=None, title="T",
                      archived=False, msg_index=1, role="assistant", snippet="s", updated=0.0)]
    md = gcs.hits_to_markdown(hits, _req(query="pk", scope="all", dataset=None),
                              datasets=["dsA", "dsB"], when=0.0)
    t = format_ts(0.0)
    mark = tr("chat.search.result.archived_mark")
    assert md.splitlines() == [
        tr("chat.search.result.header", target="dsA, dsB", time=t, n=2), "",
        (f"1. [dsB / t\\_1{mark}](chatsearch:abcdef12:3?q=pk) · {tr('chat.role.user')} · {t}"
         " — a\\*b"),
        f"2. [T](chatsearch:12345678:1?q=pk) · {tr('chat.role.assistant')} · {t} — s",
    ]


def _rhit(sid, idx=1, dataset="dsC", title="Peak chat", archived=False, score=0.0312,
          snippet="offset was 0.12", role="assistant"):
    from llm_bridge.chat_index import RecallHit
    return RecallHit(sid=sid, sid8=sid[:8], idx=idx, dataset=dataset, title=title,
                     archived=archived, role=role, score=score, cosine=0.8, lexical=0.0,
                     snippet=snippet, updated=0.0)


_SID1 = "1a2b3c4d" + "0" * 24
_SID2 = "5e6f7a8b" + "0" * 24


def _sem_req(mode="semantic", scope="registered", dataset=None, query="ベースライン ずれ",
             archived=False):
    from llm_bridge.chat_search import SearchRequest
    return SearchRequest(query=query, scope=scope, dataset=dataset, include_archived=archived,
                         ai=False, mode=mode)


def test_related_section_markdown_lines():
    from gui import chat_search as gcs
    from llm_bridge import chat_search as core
    req = _sem_req()
    hits = [_rhit(_SID1, 12, dataset="dsA", title="ベース_補正"),
            _rhit(_SID2, 3, dataset="dsC", archived=True, snippet="x" * 400, role="user")]
    md = gcs.related_section_markdown(hits, req, open_datasets=["dsA", "dsB"])
    lines = md.splitlines()
    assert len(lines) == 2
    link1 = core.make_search_link(_SID1, 12, req.query, dataset="dsA")
    assert lines[0] == (f"1. [dsA / ベース\\_補正]({link1}) · {tr('chat.role.assistant')} · "
                        f"{core.format_ts(0.0)} · 0.0312 — offset was 0.12")
    assert "&ds=dsA" in link1
    mark = tr("chat.search.result.archived_mark")
    assert lines[1].startswith(f"2. [dsC / Peak chat{mark}](")
    assert lines[1].endswith(f" · [{tr('chat.search.result.open_dataset')}]"
                             f"({core.make_open_dataset_link('dsC')})")
    assert "x" * 159 + "…" in lines[1] and "x" * 161 not in lines[1]
    # 開いている DS が分からなければ「開く」を付けない・除外・0 件
    assert "opendataset:" not in gcs.related_section_markdown(hits, req, open_datasets=None)
    only2 = gcs.related_section_markdown(hits, req, exclude={(_SID1, 12)}, open_datasets=[])
    assert only2.startswith("1. [dsC / ") and len(only2.splitlines()) == 1
    assert gcs.related_section_markdown([], req) == tr("chat.search.related.none")
    assert gcs.related_section_markdown(hits, req, exclude={(_SID1, 12), (_SID2, 3)}) == \
        tr("chat.search.related.none")


def test_related_section_markdown_caps_lines():
    from gui import chat_search as gcs
    hits = [_rhit(f"{i:08x}" + "0" * 24, i) for i in range(150)]
    md = gcs.related_section_markdown(hits, _sem_req(), open_datasets=None)
    assert len(md.splitlines()) == gcs._MAX_MD_HITS


def test_embedding_markdown_semantic_and_hybrid():
    from gui import chat_search as gcs
    from llm_bridge.chat_search import SearchHit, format_ts
    t = format_ts(0.0)
    req = _sem_req()
    md = gcs.embedding_markdown(None, req, datasets=[], when=0.0, model="ruri-v3-30m")
    assert md.splitlines() == [
        tr("chat.search.result.header_semantic",
           target=tr("chat.search.scope.registered_short"), time=t, model="ruri-v3-30m"),
        "", tr("chat.search.related.running")]
    md = gcs.embedding_markdown(None, req, datasets=[], when=0.0, model="m", related="BODY")
    assert md.endswith("\n\nBODY") and "###" not in md
    hreq = _sem_req(mode="hybrid", scope="dataset", dataset="dsA", query="peak", archived=True)
    hit = SearchHit(session_id=_SID1, dataset="dsA", title="T", archived=False, msg_index=2,
                    role="user", snippet="peak", updated=0.0)
    md = gcs.embedding_markdown([hit], hreq, datasets=["dsA"], when=0.0, model="m")
    assert md.splitlines() == [
        tr("chat.search.result.header", target="dsA", time=t, n=1)
        + tr("chat.search.result.archived_included"),
        "", "### " + tr("chat.search.section.exact"), "",
        f"1. [T](chatsearch:1a2b3c4d:2?q=peak) · {tr('chat.role.user')} · {t} — peak",
        "", "### " + tr("chat.search.section.related", model="m"), "",
        tr("chat.search.related.running")]
    md0 = gcs.embedding_markdown([], hreq, datasets=["dsA"], when=0.0, model="m", related="R")
    assert tr("chat.search.section.exact") not in md0
    assert md0.splitlines()[-3:] == ["### " + tr("chat.search.section.related", model="m"),
                                     "", "R"]


# ---- Ctrl+F の 4 モード（Issue #128）: 結果のタブ・完了・リンク ----


class _FakeRecallIndex:
    def __init__(self, hits=(), *, updated=True, stale=None, warnings=(), error=None,
                 delay=0.0):
        self.hits = list(hits)
        self.updated = updated
        self.stale = dict(stale or {})
        self.warnings = list(warnings)
        self.error = error
        self.delay = delay
        self.calls: list = []

    def recall(self, query, *, datasets=None, include_archived=False, k=20, update=False,
               budget_s=None, blocking=False):
        import time as _t
        from llm_bridge.chat_index import RecallResult
        self.calls.append({"query": query, "datasets": datasets,
                           "include_archived": include_archived, "k": k, "update": update,
                           "budget_s": budget_s, "blocking": blocking})
        if self.delay:
            _t.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return RecallResult(list(self.hits), self.updated, dict(self.stale), list(self.warnings))


@pytest.fixture()
def jobs(monkeypatch):
    """RecallJob を溜めておき、テストが job.run() で（同じスレッドで）完了させる。"""
    import gui.chat_search as gcs
    got: list = []
    monkeypatch.setattr(gcs, "_spawn", got.append)
    return got


def _ereq(query="peak", mode="semantic", scope="registered", dataset=None, archived=False):
    from llm_bridge.chat_search import SearchRequest
    return SearchRequest(query=query, scope=scope, dataset=dataset, include_archived=archived,
                         ai=False, mode=mode)


def _md_of(sess):
    return sess.messages[2].content


def test_semantic_search_registered_tab_then_related(env, monkeypatch, jobs):
    w, _ = env
    a = _add(w, "dsA", [("user", "baseline drift fixed")], title="Atitle")
    idx = _FakeRecallIndex([_rhit(_SID1, 4, dataset="dsC", title="Closed"),
                            _rhit(a.id, 1, dataset="dsA", title="Atitle")])
    _recall_env(monkeypatch, index=idx)
    emitted = []
    w.messageAdded.connect(lambda *x: emitted.append(x))
    n = len(w._sessions)
    w.run_search(_ereq(query="offset correction"))
    sess = w._active
    assert len(w._sessions) == n + 1 and sess.kind == "search" and sess.dataset == "dsA"
    assert sess.search_spec == {"scope": "all", "dataset": None, "include_archived": False,
                                "ai": False, "mode": "semantic", "registered": True}
    assert sess.messages[1].content == tr(
        "chat.search.semantic_user_line", query="offset correction",
        scope=tr("chat.search.scope.registered_short"))
    assert _md_of(sess).endswith(tr("chat.search.related.running"))
    assert len(jobs) == 1 and _history("dsA")[0] == []
    jobs[0].run()
    assert idx.calls == [{"query": "offset correction", "datasets": None,
                          "include_archived": False, "k": 100, "update": True,
                          "budget_s": 20.0, "blocking": False}]
    md = _md_of(sess)
    assert tr("chat.search.related.running") not in md
    assert "&ds=dsC" in md and "(opendataset:dsC)" in md
    assert f"chatsearch:{a.id[:8]}:1?q=offset%20correction&ds=dsA" in md
    assert md.count("(opendataset:") == 1                  # 開いている dsA には付けない
    assert [e[1] for e in emitted] == ["user", "assistant", "assistant"]
    assert emitted[-1][2] == md
    entries, _ = _history("dsA")
    e = entries[-1]
    assert (e["mode"], e["semantic"], e["exact"], e["scope"], e["registered"], e["n_hits"]) == \
        ("text", True, False, "all", True, 2)
    assert e["session_id"] == sess.id and e["datasets"] == []
    html = w._log.document().toHtml()
    assert "opendataset:dsC" in html and "&amp;ds=dsC" in html
    assert w._recall_jobs == {}


def test_hybrid_search_exact_now_related_later(env, monkeypatch, jobs):
    w, _ = env
    a = _add(w, "dsA", [("user", "peak here"), ("assistant", "peak there")], title="A")
    idx = _FakeRecallIndex([_rhit(a.id, 1, dataset="dsA", title="A"),     # 一致の節にある
                            _rhit(a.id, 9, dataset="dsA", title="A")])
    _recall_env(monkeypatch, index=idx)
    w.run_search(_ereq(query="peak", mode="hybrid", scope="dataset", dataset="dsA"))
    sess = w._active
    assert sess.search_spec == {"scope": "dataset", "dataset": "dsA", "include_archived": False,
                                "ai": False, "mode": "hybrid"}
    md0 = _md_of(sess)
    assert "### " + tr("chat.search.section.exact") in md0
    assert f"chatsearch:{a.id[:8]}:1?q=peak)" in md0 and f"chatsearch:{a.id[:8]}:2?q=peak)" in md0
    assert md0.endswith(tr("chat.search.related.running"))
    jobs[0].run()
    assert idx.calls[0]["datasets"] == ["dsA"]
    md = _md_of(sess)
    related = md.split("### " + tr("chat.search.section.related", model="ruri-v3-30m"))[1]
    assert f"chatsearch:{a.id[:8]}:9?q=peak&ds=dsA" in related
    assert f":{a.id[:8]}:1?" not in related                  # 一致の節にある (sid, idx) は除く
    assert md.startswith(md0.rsplit(tr("chat.search.related.running"), 1)[0])
    assert _history("dsA")[0][-1]["n_hits"] == 3 and _history("dsA")[0][-1]["exact"] is True


def test_embedding_search_all_scope_and_unbound_fallback(qapp, monkeypatch, dirs, jobs):
    idx = _FakeRecallIndex()
    _recall_env(monkeypatch, index=idx)
    w, _win = _make_widget(monkeypatch)
    w.run_search(_ereq(mode="semantic", scope="all"))
    jobs[-1].run()
    assert idx.calls[-1]["datasets"] == ["dsA", "dsB"]
    assert _md_of(w._active).endswith(tr("chat.search.related.none"))
    # データセットが無ければ「登録済みの全データセット」で探す（未所属のチャットは索引に無い）
    w2, win2 = _make_widget(monkeypatch, open_ds=(), current=None)
    w2.run_search(_ereq(mode="semantic", scope="unbound"))
    s2 = w2._active
    assert s2.search_spec["registered"] is True and s2.dataset is None
    jobs[-1].run()
    assert idx.calls[-1]["datasets"] is None
    win2.set_active_dataset.assert_not_called()


def test_embedding_search_failure_keeps_tab(env, monkeypatch, jobs):
    w, _ = env
    _recall_env(monkeypatch, index=_FakeRecallIndex(error=RuntimeError("onnx broke")))
    w.run_search(_ereq(mode="semantic", scope="dataset", dataset="dsA"))
    sess = w._active
    jobs[0].run()
    assert _md_of(sess).endswith(tr("chat.search.related.failed"))
    assert sess in w._sessions
    assert _history("dsA")[0][-1]["n_hits"] is None


@pytest.mark.parametrize("kw, note", [
    ({"updated": False, "stale": {"dsA": 2}}, "index_busy"),
    ({"stale": {"dsB": 1, "dsA": 2}}, "stale"),
    ({"warnings": "unreachable", "stale": {"dsA": 1}}, "unreachable"),
    ({"warnings": "shard-invalid"}, "warning"),
    ({"warnings": "budget"}, None),
    ({}, None),
])
def test_embedding_search_status_note(env, monkeypatch, jobs, kw, note):
    from llm_bridge.chat_index import IndexWarning
    w, win = env
    if "warnings" in kw:
        kw = dict(kw, warnings=[IndexWarning(kw["warnings"], "dsC", "x")])
    _recall_env(monkeypatch, index=_FakeRecallIndex(**kw))
    w.run_search(_ereq(mode="semantic", scope="dataset", dataset="dsA"))
    jobs[0].run()
    texts = _status_texts(win)
    want = {
        "index_busy": tr("chat.search.related.index_busy"),
        "stale": tr("chat.search.related.stale", datasets="dsA, dsB"),
        "unreachable": tr("chat.search.related.unreachable", datasets="dsC"),
        "warning": tr("chat.search.related.warning", detail="shard-invalid (dsC)"),
    }
    if note is None:
        assert not any(t in texts for t in want.values())
    else:
        assert want[note] in texts


def test_embedding_search_not_ready(env, monkeypatch, jobs):
    w, win = env
    _recall_env(monkeypatch, ready=False)
    n = len(w._sessions)
    w.run_search(_ereq(mode="hybrid", scope="dataset", dataset="dsA"))
    assert len(w._sessions) == n and jobs == []
    assert tr("chat.search.related.not_ready") in _status_texts(win)


def test_recall_after_tab_deleted(env, monkeypatch, jobs):
    w, _ = env
    _recall_env(monkeypatch, index=_FakeRecallIndex([_rhit(_SID1)]))
    w.run_search(_ereq(mode="semantic", scope="dataset", dataset="dsA"))
    sess = w._active
    w._sessions.remove(sess)
    w._active = w._sessions[0]
    jobs[0].run()                                         # 落ちない
    assert _history("dsA")[0] == []
    assert w._recall_jobs == {}


def test_recall_after_dataset_switch_updates_background_tab(env, monkeypatch, jobs):
    w, _ = env
    _recall_env(monkeypatch, index=_FakeRecallIndex([_rhit(_SID1)]))
    w.run_search(_ereq(mode="semantic", scope="dataset", dataset="dsA"))
    sess = w._active
    w.set_current_dataset("dsB")
    assert w._active is not sess
    before = w._log.toPlainText()
    jobs[0].run()
    assert "(opendataset:dsC)" in _md_of(sess)
    assert w._log.toPlainText() == before                 # 背景のタブは描き直さない
    assert _history("dsA")[0][-1]["session_id"] == sess.id


def test_recall_does_not_touch_changed_message(env, monkeypatch, jobs):
    from llm_backend.base import Message
    w, _ = env
    _recall_env(monkeypatch, index=_FakeRecallIndex([_rhit(_SID1)]))
    w.run_search(_ereq(mode="semantic", scope="dataset", dataset="dsA"))
    sess = w._active
    sess.messages[2] = Message(role="assistant", content="edited")
    jobs[0].run()
    assert _md_of(sess) == "edited"


def test_recall_replaces_message_object_not_text(env, monkeypatch, jobs):
    """送信中の worker が浅いコピーで持つ過去の本文を書き換えない（新しい Message に差し替える）。"""
    w, _ = env
    _recall_env(monkeypatch, index=_FakeRecallIndex([_rhit(_SID1)]))
    w.run_search(_ereq(mode="semantic", scope="dataset", dataset="dsA"))
    sess = w._active
    w._input.setPlainText("follow-up")
    w._on_send()
    worker_msgs = w._turns[sess.id].worker._messages
    initial = worker_msgs[2].content
    jobs[0].run()
    assert worker_msgs[2].content == initial
    assert _md_of(sess) != initial and "(opendataset:dsC)" in _md_of(sess)
    assert sess.messages[3].content == "follow-up"


def test_recall_dropped_while_window_shuts_down(env, monkeypatch, jobs):
    w, win = env
    _recall_env(monkeypatch, index=_FakeRecallIndex([_rhit(_SID1)]))
    w.run_search(_ereq(mode="semantic", scope="dataset", dataset="dsA"))
    sess = w._active
    initial = _md_of(sess)
    win._shutting_down = True                             # Tier 3 の 6b・終了の確定
    jobs[0].run()
    assert _md_of(sess) == initial and _history("dsA")[0] == []


def test_recall_job_real_thread_delivers_on_gui_thread(env, monkeypatch):
    import threading
    import time
    from PySide6.QtTest import QTest
    w, _ = env
    _recall_env(monkeypatch, index=_FakeRecallIndex([_rhit(_SID1)], delay=0.05))
    on_main = []
    orig = w._on_recall_finished

    def spy(*a):
        on_main.append(threading.current_thread() is threading.main_thread())
        orig(*a)

    monkeypatch.setattr(w, "_on_recall_finished", spy)
    w.run_search(_ereq(mode="semantic", scope="dataset", dataset="dsA"))
    sess = w._active
    deadline = time.monotonic() + 5.0
    while w._recall_jobs and time.monotonic() < deadline:
        QTest.qWait(20)
    assert not w._recall_jobs
    assert on_main == [True]
    assert "(opendataset:dsC)" in _md_of(sess)


def test_recall_job_parent_deleted_before_emit(qapp):
    import threading
    import shiboken6
    from PySide6.QtCore import QObject
    from PySide6.QtTest import QTest
    from gui.chat_search import RecallJob
    parent = QObject()
    job = RecallJob(lambda: "result", parent=parent)
    got = []
    job.finished.connect(got.append)
    shiboken6.delete(parent)                               # 親（ChatWidget）ごと消えた
    errors = []

    def body():
        try:
            job.run()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=body)
    t.start()
    t.join(5)
    QTest.qWait(50)
    assert errors == [] and got == []


def test_link_closed_dataset_with_ds_offers_open(env):
    w, win = env
    _click(w, "chatsearch:abcdef01:3?q=peak&ds=dsC")
    assert tr("chat.search.dataset_closed_open", dataset="dsC") in _status_texts(win)
    win.dispatch_command.assert_not_called()             # 自動では開かない
    _click(w, "chatsearch:abcdef01:3?ds=dsA")            # 開いている DS なら従来どおり
    assert tr("chat.search.link_missing") in _status_texts(win)


def test_link_closed_dataset_from_index(env, monkeypatch):
    from llm_bridge import chat_index
    w, win = env
    monkeypatch.setattr(chat_index, "dataset_of_session", lambda sid: "dsC")
    _click(w, "chatsearch:abcdef01:3")
    assert tr("chat.search.dataset_closed_open", dataset="dsC") in _status_texts(win)


def test_link_found_but_closed_dataset(env):
    w, win = env
    b = _add(w, "dsB", [("user", "peak b")])
    win.set_active_dataset.side_effect = lambda ds: False
    _click(w, f"chatsearch:{b.id[:8]}:1?ds=dsB")
    assert tr("chat.search.dataset_closed_open", dataset="dsB") in _status_texts(win)
    _click(w, f"chatsearch:{b.id[:8]}:1")                # ds 無し（一致検索のリンク）は従来どおり
    assert tr("chat.search.dataset_closed", dataset="dsB") in _status_texts(win)


def _setup_open(w, win, opened_front=True):
    open_names = ["dsA", "dsB"]
    win.open_dataset_names.side_effect = lambda: list(open_names)

    def dispatch(verb, **kw):
        open_names.append(kw["name"])
        if opened_front:
            w.set_current_dataset(kw["name"])            # open-dataset は開いた DS を前面にする
        return "restored:0"

    win.dispatch_command.side_effect = dispatch
    win.has_command.return_value = True


def test_open_dataset_link_opens_and_returns_to_results(env, monkeypatch, jobs):
    w, win = env
    _recall_env(monkeypatch, index=_FakeRecallIndex([_rhit(_SID1, dataset="dsC")]))
    w.run_search(_ereq(mode="semantic", scope="dataset", dataset="dsA"))
    sess = w._active
    jobs[0].run()
    _setup_open(w, win)
    _click(w, "opendataset:dsC")
    win.dispatch_command.assert_called_once_with("open-dataset", name="dsC")
    assert w._current_dataset == "dsA" and w._active is sess
    assert tr("chat.search.dataset_opened", dataset="dsC") in _status_texts(win)


def test_open_dataset_link_already_open_does_not_dispatch(env):
    w, win = env
    win.has_command.return_value = True
    _click(w, "opendataset:dsB")
    win.dispatch_command.assert_not_called()
    assert tr("chat.search.dataset_opened", dataset="dsB") in _status_texts(win)


@pytest.mark.parametrize("res", ["error:dsC", RuntimeError("boom")])
def test_open_dataset_link_failure(env, res):
    w, win = env
    win.has_command.return_value = True
    if isinstance(res, Exception):
        win.dispatch_command.side_effect = res
    else:
        win.dispatch_command.return_value = res
    _click(w, "opendataset:dsC")
    assert tr("chat.search.open_failed", dataset="dsC") in _status_texts(win)
    assert tr("chat.search.dataset_opened", dataset="dsC") not in _status_texts(win)


def test_open_dataset_link_unreadable_session_warns(env, monkeypatch):
    import gui.chat as gchat
    w, win = env
    win.has_command.return_value = True
    win.dispatch_command.return_value = "unreadable-session:dsC"
    warned = []
    monkeypatch.setattr(gchat.QMessageBox, "warning", lambda *a: warned.append(a[1:]))
    _click(w, "opendataset:dsC")
    assert warned == [(tr("err.session_unreadable.title"),
                       tr("err.session_unreadable.body", dataset="dsC"))]
    assert tr("chat.search.dataset_opened", dataset="dsC") in _status_texts(win)


def test_open_dataset_link_without_command(env):
    w, win = env
    win.has_command.return_value = False
    _click(w, "opendataset:dsC")
    win.dispatch_command.assert_not_called()
    assert tr("chat.search.open_failed", dataset="dsC") in _status_texts(win)


def test_open_dataset_link_name_roundtrip_through_qurl(env):
    from llm_bridge import chat_search as core
    w, win = env
    win.has_command.return_value = True
    win.dispatch_command.return_value = "restored:0"
    for name in ("測定 2026", "a&b=c", "x/y?z#w"):
        _click(w, core.make_open_dataset_link(name))
        assert win.dispatch_command.call_args.kwargs == {"name": name}


def test_session_summaries_search_mode(env, monkeypatch, jobs):
    w, _ = env
    _add(w, "dsA", [("user", "peak")])
    _recall_env(monkeypatch, index=_FakeRecallIndex())
    w.run_search(_req())
    text = w._active
    w.run_search(_ereq(mode="hybrid", scope="dataset", dataset="dsA"))
    hyb = w._active
    rows = {r["id"]: r for r in w.session_summaries()}
    assert rows[text.id]["search_mode"] == "text"
    assert rows[hyb.id]["search_mode"] == "hybrid"
    plain = next(s for s in w._sessions if s.kind == "chat")
    assert rows[plain.id]["search_mode"] is None
