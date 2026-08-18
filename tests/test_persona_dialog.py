"""Tests for gui.persona_dialog (offscreen).

ストアはインメモリ dict を gui.persona_dialog の**モジュール名**に monkeypatch
する（PersonaDialog がストア関数をモジュールスコープ import している前提の検証を
兼ねる）。実ファイル personas.json / ui_prefs.json は conftest の autouse fixture
で tmp へ隔離済みだが、ここでは触らない。
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture()
def parent_widget(qapp):
    from PySide6.QtWidgets import QWidget

    return QWidget()


class _MainWindowStub:
    """PersonaDialog が呼ぶ転送ヘルパだけを持つ main window スタブ。"""

    def __init__(self, default="", busy=False):
        self._default = default
        self._busy = busy
        self.applied: list[str] = []

    def _persona_default(self) -> str:
        return self._default

    def _set_persona_default(self, name: str) -> bool:
        self.applied.append(name)
        return not self._busy


class _ChatStub:
    """SessionPersonaDialog が呼ぶ ChatWidget 面だけのスタブ。"""

    def __init__(self, default=""):
        self._default = default
        self.calls: list[tuple[object, object]] = []

    def persona_default(self) -> str:
        return self._default

    def _set_session_persona(self, sess, value) -> None:
        self.calls.append((sess, value))


@pytest.fixture()
def store(monkeypatch):
    """インメモリ辞書ストア（gui.persona_dialog のモジュール名を差し替える）。"""
    import gui.persona_dialog as mod
    from llm_bridge.personas import Persona

    data = {"A": "text-a", "B": "text-b"}

    monkeypatch.setattr(
        mod, "list_personas", lambda: [Persona(n, t) for n, t in data.items()]
    )
    monkeypatch.setattr(
        mod, "get_persona", lambda n: Persona(n, data[n]) if n in data else None
    )

    def _upsert(name, text):
        data[name] = text
        return True

    def _delete(name):
        data.pop(name, None)
        return True

    monkeypatch.setattr(mod, "upsert_persona", _upsert)
    monkeypatch.setattr(mod, "delete_persona", _delete)
    return data


def _dialog(parent_widget, *, default="", busy=False):
    import gui.persona_dialog as mod

    mw = _MainWindowStub(default=default, busy=busy)
    return mod.PersonaDialog(mw, parent_widget), mw


# --------------------------------------------------------------------------- #
# PersonaDialog（全体設定）                                                    #
# --------------------------------------------------------------------------- #


def test_none_first_and_global_selection_seeded(store, parent_widget):
    from common.i18n import tr

    dlg, _ = _dialog(parent_widget, default="B")
    assert dlg._combo.itemText(0) == tr("persona.dialog.none")
    assert dlg._combo.itemData(0) == ""
    assert dlg._combo.currentData() == "B"
    assert dlg._editor.toPlainText() == "text-b"


def test_none_disables_editor_and_delete(store, parent_widget):
    dlg, _ = _dialog(parent_widget, default="")
    assert dlg._combo.currentIndex() == 0
    assert not dlg._editor.isEnabled()
    assert not dlg._delete_btn.isEnabled()
    dlg._combo.setCurrentIndex(dlg._combo.findData("A"))
    assert dlg._editor.isEnabled()
    assert dlg._delete_btn.isEnabled()


def test_dangling_pref_falls_back_to_none(store, parent_widget):
    """pref が指す名前がストアに無い（別 PC 由来の dangling）→（なし）に落ちる。
    適用しない限り pref は触らない（never-rewrite）。"""
    dlg, mw = _dialog(parent_widget, default="消えた名前")
    assert dlg._combo.currentIndex() == 0
    assert dlg._combo.currentData() == ""
    assert mw.applied == []


def test_add_persists_immediately_and_survives_cancel(
    store, parent_widget, monkeypatch
):
    import gui.persona_dialog as mod

    monkeypatch.setattr(
        mod.QInputDialog, "getText", staticmethod(lambda *a, **k: ("新入り", True))
    )
    dlg, _ = _dialog(parent_widget)
    dlg._on_add()
    assert store["新入り"] == ""                 # 即時永続
    assert dlg._combo.currentData() == "新入り"   # 追加後は選択＋editor 編集可
    assert dlg._editor.isEnabled()
    assert dlg.definitions_changed
    dlg.reject()                                  # Cancel しても定義は残る
    assert "新入り" in store


def test_reserved_duplicate_and_blank_names_rejected(
    store, parent_widget, monkeypatch
):
    import gui.persona_dialog as mod

    warnings = []
    monkeypatch.setattr(
        mod.QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a))
    )
    for bad in ("（なし）", "(none)", "A"):        # 予約名 ×2・重複名
        monkeypatch.setattr(
            mod.QInputDialog, "getText",
            staticmethod(lambda *a, _b=bad, **k: (_b, True)),
        )
        dlg, _ = _dialog(parent_widget)
        dlg._on_add()
    assert len(warnings) == 3
    # 空は無言 no-op（warning も出さない）
    monkeypatch.setattr(
        mod.QInputDialog, "getText", staticmethod(lambda *a, **k: ("   ", True))
    )
    dlg, _ = _dialog(parent_widget)
    dlg._on_add()
    assert len(warnings) == 3
    assert set(store) == {"A", "B"}


def test_reserved_names_cover_all_catalogs():
    """_RESERVED_NAMES は全カタログの none ラベルを網羅する（カタログ追加時に
    このテストが落ちて frozenset の更新を強制する）。"""
    import tomllib

    from common.paths import i18n_dir
    from gui.persona_dialog import _RESERVED_NAMES

    paths = sorted(i18n_dir().glob("*.toml"))
    assert paths, "no i18n catalogs found"
    for p in paths:
        with open(p, "rb") as f:
            cat = tomllib.load(f)
        assert cat["persona.dialog.none"] in _RESERVED_NAMES, p.name


def test_combo_switch_commits_body_edit(store, parent_widget):
    dlg, _ = _dialog(parent_widget, default="A")
    dlg._editor.setPlainText("edited-a")
    dlg._combo.setCurrentIndex(dlg._combo.findData("B"))
    assert store["A"] == "edited-a"               # 切替境界で即保存
    assert dlg.definitions_changed
    assert dlg._editor.toPlainText() == "text-b"  # 切替先を読込済み


def test_commit_failure_reverts_switch_and_keeps_text(
    store, parent_widget, monkeypatch
):
    import gui.persona_dialog as mod
    from common.i18n import tr

    dlg, _ = _dialog(parent_widget, default="A")
    monkeypatch.setattr(mod, "upsert_persona", lambda n, t: False)
    dlg._editor.setPlainText("編集中テキスト")
    dlg._combo.setCurrentIndex(dlg._combo.findData("B"))
    assert dlg._combo.currentData() == "A"                 # 巻き戻し
    assert dlg._editor.toPlainText() == "編集中テキスト"    # テキスト保持
    assert dlg._status.text() == tr("persona.dialog.save_failed")
    assert not dlg.definitions_changed


def test_cancel_discards_only_uncommitted_edit(store, parent_widget):
    dlg, _ = _dialog(parent_widget, default="A")
    dlg._editor.setPlainText("未コミット編集")
    dlg.reject()
    assert store["A"] == "text-a"                 # Cancel は commit しない
    assert not dlg.definitions_changed


def test_apply_commits_and_applies_selection(store, parent_widget):
    from PySide6.QtWidgets import QDialog

    dlg, mw = _dialog(parent_widget, default="A")
    dlg._editor.setPlainText("apply-edit")
    dlg._on_apply()
    assert store["A"] == "apply-edit"             # 適用境界でも commit
    assert mw.applied == ["A"]
    assert dlg.apply_was_busy is False
    assert dlg.result() == QDialog.DialogCode.Accepted

    dlg2, mw2 = _dialog(parent_widget, default="A", busy=True)
    dlg2._on_apply()
    assert mw2.applied == ["A"]
    assert dlg2.apply_was_busy is True            # 応答中 → 次送信から反映


def test_apply_aborts_on_commit_failure(store, parent_widget, monkeypatch):
    import gui.persona_dialog as mod
    from PySide6.QtWidgets import QDialog

    dlg, mw = _dialog(parent_widget, default="A")
    monkeypatch.setattr(mod, "upsert_persona", lambda n, t: False)
    dlg._editor.setPlainText("保存できない編集")
    dlg._on_apply()
    assert mw.applied == []                       # 選択は反映されない
    assert dlg.result() != QDialog.DialogCode.Accepted   # 開いたまま


def test_apply_none_selection_writes_empty_string(store, parent_widget):
    dlg, mw = _dialog(parent_widget, default="B")
    dlg._combo.setCurrentIndex(0)
    dlg._on_apply()
    assert mw.applied == [""]


def test_delete_confirms_then_falls_back_to_none(store, parent_widget, monkeypatch):
    import gui.persona_dialog as mod
    from PySide6.QtWidgets import QMessageBox

    dlg, _ = _dialog(parent_widget, default="A")
    # No → 削除しない
    monkeypatch.setattr(
        mod.QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.No),
    )
    dlg._on_delete()
    assert "A" in store
    assert not dlg.definitions_changed
    # Yes → 削除・（なし）フォールバック・editor 無効化
    monkeypatch.setattr(
        mod.QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    dlg._on_delete()
    assert "A" not in store
    assert dlg._combo.currentData() == ""
    assert not dlg._editor.isEnabled()
    assert dlg.definitions_changed


# --------------------------------------------------------------------------- #
# SessionPersonaDialog（タブ単位・選択のみ）                                   #
# --------------------------------------------------------------------------- #


def _session_dlg(parent_widget, *, persona="__unset__", chat_default=""):
    import gui.persona_dialog as mod
    from llm_bridge import chat_store

    sess = chat_store.new_session("mock", "sys")
    if persona != "__unset__":
        sess.persona = persona
    chat = _ChatStub(default=chat_default)
    dlg = mod.SessionPersonaDialog(_MainWindowStub(), chat, sess, parent_widget)
    return dlg, sess, chat


def test_session_follow_seed_shows_global_name(store, parent_widget):
    dlg, _, _ = _session_dlg(parent_widget, chat_default="B")
    assert dlg._follow_default.isChecked()
    assert dlg._combo.currentData() == "B"


def test_session_name_and_explicit_none_seed(store, parent_widget):
    dlg, _, _ = _session_dlg(parent_widget, persona="A", chat_default="B")
    assert not dlg._follow_default.isChecked()
    assert dlg._combo.currentData() == "A"

    dlg2, _, _ = _session_dlg(parent_widget, persona="", chat_default="B")
    assert not dlg2._follow_default.isChecked()
    assert dlg2._combo.currentIndex() == 0
    assert dlg2._combo.currentData() == ""


def test_session_ghost_name_inserted_at_index1(store, parent_widget):
    """ストアに無い名前（別 PC 由来など）を不可視にしない: index 1 に挿入して選択。"""
    dlg, _, _ = _session_dlg(parent_widget, persona="幽霊ペルソナ")
    assert dlg._combo.currentIndex() == 1
    assert dlg._combo.itemText(1) == "幽霊ペルソナ"
    assert dlg._combo.currentData() == "幽霊ペルソナ"


def test_session_follow_gates_combo_only(store, parent_widget):
    from PySide6.QtWidgets import QDialogButtonBox

    dlg, _, _ = _session_dlg(parent_widget, chat_default="A")
    assert dlg._follow_default.isChecked()
    assert not dlg._combo.isEnabled()
    # 「既定に従う」の適用（=上書き解除）を可能に保つため OK は常に有効
    assert dlg._buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    dlg._follow_default.setChecked(False)
    assert dlg._combo.isEnabled()


def test_session_apply_three_state_mapping(store, parent_widget):
    # checked → None（全体既定に従う）
    dlg, sess, chat = _session_dlg(parent_widget, persona="A")
    dlg._follow_default.setChecked(True)
    dlg._on_apply()
    assert chat.calls == [(sess, None)]

    # unchecked + （なし） → ""（明示的になし）
    dlg2, sess2, chat2 = _session_dlg(parent_widget)
    dlg2._follow_default.setChecked(False)
    dlg2._combo.setCurrentIndex(0)
    dlg2._on_apply()
    assert chat2.calls == [(sess2, "")]

    # unchecked + 名前 → その名前
    dlg3, sess3, chat3 = _session_dlg(parent_widget)
    dlg3._follow_default.setChecked(False)
    dlg3._combo.setCurrentIndex(dlg3._combo.findData("B"))
    dlg3._on_apply()
    assert chat3.calls == [(sess3, "B")]
