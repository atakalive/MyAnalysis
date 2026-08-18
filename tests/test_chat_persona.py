"""ChatWidget のペルソナ表示（_persona_header / transcript）のテスト。

既定（ペルソナなし）の transcript がペルソナ機能導入前とバイト同一に保たれること
が中核の回帰ガード。定義ストア（personas.json）と ui_prefs は conftest の autouse
fixture で tmp へ隔離済み。

GUI テストの常: QT_QPA_PLATFORM=offscreen + qapp fixture。context menu 経由の
テスト（chat.menu.persona → SessionPersonaDialog）は後続でこのファイルに追記する。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from common.i18n import tr


class _FakeBackend:
    name = "mock"
    model = "fake-model"

    def set_persona(self, value: str) -> None:   # ChatWidget の duck-typed 注入先
        pass


@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def widget(qapp):
    from gui.chat import ChatWidget

    w = ChatWidget(_FakeBackend, dispatch=lambda *a, **k: None)
    w.bind_window(MagicMock())
    return w


def _make_session(widget, *, dataset=None, title="テスト"):
    from llm_bridge import chat_store

    sess = chat_store.new_session("mock", "sys", dataset=dataset, title=title)
    widget._sessions.append(sess)
    widget._current_dataset = dataset
    widget._rebuild_tab_bar()
    return sess


# ---- _persona_header マトリクス ----


def test_default_none_no_persona_line_byte_identical(widget, monkeypatch):
    """既定（全体既定なし・上書きなし）ではヘッダ行を一切足さない — フックを
    無効化した描画（機能導入前相当）と HTML がバイト同一。"""
    sess = _make_session(widget)
    assert widget._persona_header(sess) is None
    widget._render_session(sess)
    with_hook = widget._log.toHtml()
    assert "persona:" not in widget._log.toPlainText()

    monkeypatch.setattr(widget, "_persona_header", lambda s: None)
    widget._render_session(sess)
    assert widget._log.toHtml() == with_hook


def test_global_name_marks_default(widget):
    from llm_bridge.personas import upsert_persona
    assert upsert_persona("全体P", "t")
    widget._persona_default = "全体P"
    sess = _make_session(widget)
    line = widget._persona_header(sess)
    assert line == f"persona: 全体P  [{tr('chat.engine.mark_default')}]"
    widget._active = sess
    widget._render_session(sess)
    # QTextBrowser は HTML 描画で連続空白を潰すので、行全体の完全一致は上で担保し
    # transcript には構成要素の出現だけを見る。
    text = widget._log.toPlainText()
    assert "persona: 全体P" in text
    assert tr("chat.engine.mark_default") in text


def test_override_name_marks_override(widget):
    from llm_bridge.personas import upsert_persona
    assert upsert_persona("個別P", "t")
    sess = _make_session(widget)
    sess.persona = "個別P"
    assert widget._persona_header(sess) \
        == f"persona: 個別P  [{tr('chat.engine.mark_override')}]"


def test_explicit_none_suppresses_global(widget):
    """明示的になし（""）は全体既定が実在名でも行を出さない。"""
    from llm_bridge.personas import upsert_persona
    assert upsert_persona("全体P", "t")
    widget._persona_default = "全体P"
    sess = _make_session(widget)
    sess.persona = ""
    assert widget._persona_header(sess) is None
    widget._render_session(sess)
    assert "persona:" not in widget._log.toPlainText()


def test_unresolvable_names_render_nothing(widget):
    """ストアに無い名前（別 PC 由来など）は上書き・全体既定のどちらでも行を
    出さない（degrade — フィールドは書き換えない）。"""
    sess = _make_session(widget)
    sess.persona = "未定義の個別名"
    assert widget._persona_header(sess) is None
    assert sess.persona == "未定義の個別名"          # never-rewrite

    sess2 = _make_session(widget)
    widget._persona_default = "未定義の全体名"
    assert widget._persona_header(sess2) is None
    widget._render_session(sess2)
    assert "persona:" not in widget._log.toPlainText()


# ---- context menu（chat.menu.persona → SessionPersonaDialog） ----


def _open_context_menu(widget, monkeypatch):
    """geometry 非依存で _on_tab_context_menu を発火させ、生成された menu を返す。

    gui.chat.QMenu を「exec は no-op・addAction は（実 QMenu の action リストに）
    記録」のキャプチャフェイクへ monkeypatch する（モーダルに開けない。実 QMenu
    のサブクラスなので addMenu / QActionGroup もそのまま動く）。加えて
    _tab_bar.tabAt を index 0 固定に patch する — offscreen では実座標がタブに
    当たらず、:index<0 ガードで抜けてしまうため。
    """
    import gui.chat as chat_mod
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QMenu

    created = []

    class _CapturingMenu(QMenu):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            created.append(self)

        def exec(self, *a, **k):      # モーダル表示はしない
            return None

    monkeypatch.setattr(chat_mod, "QMenu", _CapturingMenu)
    monkeypatch.setattr(widget._tab_bar, "tabAt", lambda pos: 0)
    widget._on_tab_context_menu(QPoint(0, 0))
    assert created, "context menu was not built"
    return created[0]


def test_context_menu_has_persona_action_after_model(widget, monkeypatch):
    _make_session(widget, title="右クリ対象")
    menu = _open_context_menu(widget, monkeypatch)
    texts = [a.text() for a in menu.actions()]
    assert tr("chat.menu.persona") in texts
    # モデル設定の直後（engine 上書きの隣に並ぶ per-session 設定）
    i_model = texts.index(tr("chat.menu.model"))
    assert texts[i_model + 1] == tr("chat.menu.persona")


def test_persona_action_opens_dialog_for_right_clicked_session(widget, monkeypatch):
    _make_session(widget, title="右クリ対象")
    target = widget._session_by_id(widget._tab_bar.tabData(0))
    assert target is not None
    opened = []
    monkeypatch.setattr(
        widget, "_open_session_persona_dialog", lambda s: opened.append(s)
    )
    menu = _open_context_menu(widget, monkeypatch)
    persona_action = next(
        a for a in menu.actions() if a.text() == tr("chat.menu.persona")
    )
    persona_action.trigger()
    assert opened == [target]
