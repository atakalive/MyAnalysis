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
    d._ai_check.setChecked(False)
    assert not d._search_btn.isEnabled()
    d._query_edit.setText("peak offset")
    assert d._search_btn.isEnabled()
    d._ai_check.setChecked(True)
    assert d._mode_stack.currentIndex() == 1
    assert d._ai_query_edit.toPlainText() == "peak offset"
    d._ai_query_edit.setPlainText("")
    assert not d._search_btn.isEnabled()
    d._ai_query_edit.setPlainText("line one\nline two")
    d._hint_edit.setText("h1 h2")
    assert d._search_btn.isEnabled()
    assert d.request().hint == "h1 h2" and d.request().ai
    d._ai_check.setChecked(False)
    assert d._mode_stack.currentIndex() == 0
    assert d._query_edit.text() == "line one line two"
    assert d.request().hint == ""


def test_dialog_ctrl_enter_accepts(env):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QDialog
    w, _ = env
    d = _dialog(w)
    d._ai_check.setChecked(True)
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
    d._ai_check.setChecked(True)
    d._ai_query_edit.setPlainText("q")
    d.accept()
    assert read_ui_pref("chat_search_scope", None) == "all"
    assert read_ui_pref("chat_search_archived", None) is True
    assert read_ui_pref("chat_search_ai", None) is True
    d2 = _dialog(w)
    assert d2._scope_combo.currentIndex() == 2
    assert d2._archived_check.isChecked() and d2._ai_check.isChecked()
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
    d._ai_check.setChecked(False)
    d._archived_check.setChecked(False)
    tree = d._history_tree
    tree.setCurrentItem(tree.topLevelItem(1))    # 標準
    assert not d._ai_check.isChecked()
    assert d._query_edit.text() == "peak"
    assert d._archived_check.isChecked()
    assert tree.topLevelItemCount() == 2
    tree.setCurrentItem(tree.topLevelItem(0))    # AI
    assert d._ai_check.isChecked()
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
