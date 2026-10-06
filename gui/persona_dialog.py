"""AI ペルソナ設定ダイアログ（全体設定 + チャットタブ単位の上書き）。

設定 menu → 「AIペルソナ…」(PersonaDialog) が全体既定の選択と定義の作成・編集・
削除を担い、チャットタブ右クリック → 「このチャットのペルソナ…」
(SessionPersonaDialog) がセッション単位の上書き（選択のみ）を担う。

規律:

- **選択は適用時のみ反映、定義編集は境界で即保存**（combo 切替・追加・削除・
  適用の各境界で ``_commit_body_edit``。backend_selector の追加/削除による候補リスト
  即時保存と同じ — Cancel しても保存済みの定義編集は残る）。
- **名前変更は v1 では非対応**: セッション上書き・全体 pref に残る dangling
  参照を fixup する機構が無いため（削除＋追加で代替する）。

ストア関数（llm_bridge.personas）は**モジュールスコープで import** する —
テストが ``gui.persona_dialog.upsert_persona`` 等を monkeypatch する前提。
ストア API は never-raise の bool 契約なので QThread（ping 相当）は無い。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from common.i18n import tr
from llm_bridge.personas import (
    delete_persona,
    get_persona,
    list_personas,
    upsert_persona,
)

# 全カタログの「なし」ラベル。ペルソナ名として許すと combo 上で予約 item0 と
# 区別できなくなるため追加時に拒否する（全カタログ網羅は test_persona_dialog の
# reserved-names テストが i18n/*.toml を舐めて担保する）。旧ラベルも紛らわしいので
# 予約に残す。
_RESERVED_NAMES = frozenset({
    "無し（お客様対応窓口）", "(none — customer-service tone)",
    "（なし）", "(none)",
})


class PersonaDialog(QDialog):
    """全体設定: ペルソナ選択（適用時のみ反映）＋定義の作成・編集・削除（境界で即保存）。

    ``definitions_changed`` は upsert/delete が成功した時点で True になり、Cancel
    されても呼び出し側（ToolWindow._open_persona_settings）が apply_persona_change
    でキャッシュ済みバックエンドへ反映する。``apply_was_busy`` は適用時に応答中の
    チャットがあった（次送信から反映）ことを示す。
    """

    def __init__(self, main_window, parent=None):
        super().__init__(parent if parent is not None else main_window)
        self._main_window = main_window
        self.setWindowTitle(tr("persona.dialog.title"))
        self.definitions_changed = False
        self.apply_was_busy = False
        # editor が表示している定義（"" = （なし）＝編集対象なし）と、その最後に
        # 保存/読込したテキスト。commit の差分判定に使う。
        self._editing_name = ""
        self._loaded_text = ""
        self._prev_index = 0

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self._combo = QComboBox(self)
        row = QWidget(self)
        box = QHBoxLayout(row)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self._combo, 1)
        self._add_btn = QPushButton(tr("persona.dialog.add"), row)
        self._add_btn.setAutoDefault(False)   # else Enter in the dialog would fire it
        self._add_btn.clicked.connect(self._on_add)
        box.addWidget(self._add_btn)
        self._delete_btn = QPushButton(tr("persona.dialog.delete"), row)
        self._delete_btn.setAutoDefault(False)
        self._delete_btn.clicked.connect(self._on_delete)
        box.addWidget(self._delete_btn)
        form.addRow(tr("persona.dialog.persona"), row)

        self._editor = QPlainTextEdit(self)
        form.addRow(tr("persona.dialog.body"), self._editor)

        self._status = QLabel("", self)
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            tr("persona.dialog.apply")
        )
        layout.addWidget(self._buttons)

        self._populate(self._seed_name())
        self._combo.currentIndexChanged.connect(self._on_selection_changed)
        self._buttons.accepted.connect(self._on_apply)
        self._buttons.rejected.connect(self.reject)

    # ----- seeding / population -----

    def _seed_name(self) -> str:
        """開いた時点の全体既定名。転送ヘルパ不在（旧ビルド window）は（なし）扱い。"""
        fn = getattr(self._main_window, "_persona_default", None)
        try:
            return (fn() or "").strip() if callable(fn) else ""
        except Exception:
            return ""

    def _populate(self, select: str) -> None:
        """combo を（なし）+ 全定義で再構築し *select*（"" = なし）を選択する。

        pref が指す名前がストアに無い（dangling — 別 PC 由来など）場合は（なし）に
        落ちる。適用は「見えている状態を書く」ので、ユーザーが適用しない限り pref
        は触らない（never-rewrite）。
        """
        self._combo.blockSignals(True)
        self._combo.clear()
        self._combo.addItem(tr("persona.dialog.none"), "")
        for p in list_personas():
            self._combo.addItem(p.name, p.name)
        idx = self._combo.findData(select)
        self._combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._combo.blockSignals(False)
        self._prev_index = self._combo.currentIndex()
        self._load_selection()

    def _load_selection(self) -> None:
        """combo の現在選択を editor に読み込む（（なし）は editor/削除を無効化）。"""
        name = self._combo.currentData() or ""
        self._editing_name = name
        if not name:
            self._loaded_text = ""
            self._editor.setPlainText("")
            self._editor.setEnabled(False)
            self._delete_btn.setEnabled(False)
            return
        p = get_persona(name)
        self._loaded_text = p.text if p is not None else ""
        self._editor.setPlainText(self._loaded_text)
        self._editor.setEnabled(True)
        self._delete_btn.setEnabled(True)

    # ----- definition edits (committed at boundaries) -----

    def _commit_body_edit(self) -> bool:
        """editor の内容を編集対象の定義へ保存する（差分がなければ書かない）。

        False = 保存失敗（status に save_failed を表示。呼び出し側は操作を中断し、
        editor のテキストは保持する — ユーザー編集を消さない）。
        """
        name = self._editing_name
        if not name:
            return True
        text = self._editor.toPlainText()
        if text == self._loaded_text:
            return True
        if not upsert_persona(name, text):
            self._status.setText(tr("persona.dialog.save_failed"))
            return False
        self._loaded_text = text
        self.definitions_changed = True
        self._status.setText("")
        return True

    def _on_selection_changed(self, idx: int) -> None:
        if not self._commit_body_edit():
            # 保存失敗 → 切替を巻き戻す（テキストは保持したまま）。blockSignals で
            # 巻き戻し自体が再入しないようにする。
            self._combo.blockSignals(True)
            self._combo.setCurrentIndex(self._prev_index)
            self._combo.blockSignals(False)
            return
        self._prev_index = idx
        self._load_selection()

    def _on_add(self) -> None:
        if not self._commit_body_edit():
            return
        name, ok = QInputDialog.getText(
            self, tr("persona.dialog.add_title"), tr("persona.dialog.add_label")
        )
        if not ok:
            return
        name = name.strip()
        if not name:
            return                                   # 空は無言 no-op
        if name in _RESERVED_NAMES:
            QMessageBox.warning(
                self, tr("persona.dialog.add_title"),
                tr("persona.dialog.name_reserved", name=name),
            )
            return
        if get_persona(name) is not None:
            QMessageBox.warning(
                self, tr("persona.dialog.add_title"),
                tr("persona.dialog.name_exists", name=name),
            )
            return
        if not upsert_persona(name, ""):             # 即時永続（Cancel しても残る）
            self._status.setText(tr("persona.dialog.save_failed"))
            return
        self.definitions_changed = True
        self._status.setText("")
        self._populate(name)
        self._editor.setFocus()

    def _on_delete(self) -> None:
        name = self._combo.currentData() or ""
        if not name:
            return                                   # （なし）は削除対象外
        if not self._commit_body_edit():
            return
        resp = QMessageBox.question(
            self, tr("persona.dialog.delete_title"),
            tr("persona.dialog.delete_confirm", name=name),
        )
        if resp != QMessageBox.StandardButton.Yes:
            return
        if not delete_persona(name):
            self._status.setText(tr("persona.dialog.save_failed"))
            return
        self.definitions_changed = True
        self._status.setText("")
        # （なし）へフォールバック。適用済み全体既定の削除でも ui_prefs は触らない
        # （適用時に見えている状態を書く。それまでは未解決名→なし degrade が支配）。
        self._populate("")

    # ----- apply -----

    def _on_apply(self) -> None:
        if not self._commit_body_edit():
            return
        # widget/メソッド不在時の転送ヘルパは True を返す（falsy を busy と誤読して
        # applied_busy を表示させないため — busy=False は chat widget の実応答のみ）。
        self.apply_was_busy = not self._main_window._set_persona_default(
            self._combo.currentData() or ""
        )
        self.accept()


class SessionPersonaDialog(QDialog):
    """タブ単位のペルソナ上書き（選択のみ・本文編集なし）。

    本文編集を載せないのは意図的: 共有ストアの編集がこのチャットのスコープに
    見える誤誘導を避ける（編集は 設定 → AIペルソナ… へヒントで誘導）。
    SessionEngineDialog には統合しない — 「どう走らせるか」と「どう喋るか」は
    別問題で、1 つの「全体設定に従う」checkbox が 2 直交上書きを兼ねられない。

    checkbox は combo だけをゲートする。適用は常に有効（上書き解除を可能に保つ。
    ping ロックが無いので AND ゲートは不要）。
    """

    def __init__(self, main_window, chat_widget, session, parent=None):
        super().__init__(parent if parent is not None else main_window)
        self._chat = chat_widget
        self._session = session
        self.setWindowTitle(tr("persona.dialog.session_title"))

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self._follow_default = QCheckBox(tr("persona.dialog.follow_default"), self)
        form.addRow("", self._follow_default)

        self._combo = QComboBox(self)
        self._combo.addItem(tr("persona.dialog.none"), "")
        for p in list_personas():
            self._combo.addItem(p.name, p.name)
        form.addRow(tr("persona.dialog.persona"), self._combo)

        hint = QLabel(tr("persona.dialog.edit_hint"), self)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            tr("persona.dialog.apply")
        )
        layout.addWidget(self._buttons)

        # シード: getattr 必須（ホットリロード patch 後の旧インスタンスに persona
        # フィールドが無い — _effective_persona_name と同じ理由）。
        ov = getattr(session, "persona", None)
        if isinstance(ov, str):
            self._follow_default.setChecked(False)
            select = ov.strip()
        else:
            self._follow_default.setChecked(True)
            select = self._global_default_name()
        idx = self._combo.findData(select)
        if idx < 0:
            # ストアに無い名前（別 PC 由来など）を不可視にしない: index 1 に挿入して
            # 選択する（never-rewrite — 適用しない限りフィールドは保持される）。
            self._combo.insertItem(1, select, select)
            idx = 1
        self._combo.setCurrentIndex(idx)

        self._follow_default.toggled.connect(self._on_follow_toggled)
        self._on_follow_toggled()
        self._buttons.accepted.connect(self._on_apply)
        self._buttons.rejected.connect(self.reject)

    def _global_default_name(self) -> str:
        """全体既定の現在名（follow 時の combo シード）。widget 不在は（なし）扱い。"""
        fn = getattr(self._chat, "persona_default", None)
        try:
            return (fn() or "").strip() if callable(fn) else ""
        except Exception:
            return ""

    def _on_follow_toggled(self, _checked: bool = False) -> None:
        # checkbox は combo だけをゲートする（適用は常に有効 — 上書き解除を可能に）。
        self._combo.setEnabled(not self._follow_default.isChecked())

    def _on_apply(self) -> None:
        if self._follow_default.isChecked():
            self._chat._set_session_persona(self._session, None)
        else:
            self._chat._set_session_persona(
                self._session, self._combo.currentData() or ""
            )
        self.accept()
