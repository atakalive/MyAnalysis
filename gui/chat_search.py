"""Chat search dialog (Ctrl+F) and result helpers — Issue #108.

検索ウィンドウ（語・対象・アーカイブ・AI 検索・検索履歴）と、標準検索の結果
Markdown、検索履歴の記録。検索そのものは Qt 非依存の llm_bridge.chat_search。

このモジュールは gui.chat を import しない（gui.chat が関数内でこちらを import する）。
テストが差し替えるので ai_search_engine / current_engine_id / current_model は
モジュール属性として持つ。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QEvent, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from common.i18n import tr
from llm_backend.engines import current_engine_id, current_model, engine_by_id, engine_label
from llm_bridge import chat_search as core
from llm_bridge import chat_search_history as history
from llm_bridge.chat_search import SearchRequest

_MAX_MD_HITS = 100
_HISTORY_COLS = 6
_COL_DATASET = 5


# ----- engine for AI search -----


def ai_search_engine():
    """(engine, model, provider) of [chat_search]; (None, "", "") = follow the global."""
    try:
        from llm_backend.engines import chat_search_selection
        return chat_search_selection()
    except Exception:
        return (None, "", "")


def ai_engine_label_text() -> str:
    try:
        eng, model, _ = ai_search_engine()
        if eng is not None:
            return tr("chat.search.dialog.ai_engine", engine=engine_label(eng),
                      model=model.strip() or current_model(eng) or "-",
                      mark=tr("chat.engine.mark_override"))
        e = engine_by_id(current_engine_id())
        return tr("chat.search.dialog.ai_engine",
                  engine=engine_label(e) if e is not None else "-",
                  model=(current_model(e) or "-") if e is not None else "-",
                  mark=tr("chat.engine.mark_default"))
    except Exception:
        return tr("chat.search.dialog.ai_engine", engine="-", model="-",
                  mark=tr("chat.engine.mark_default"))


# ----- result Markdown -----


def scope_label(req) -> str:
    if req.scope == "all":
        return tr("chat.search.scope.all_short")
    if req.scope == "unbound" or req.dataset is None:
        return tr("chat.search.scope.none")
    return req.dataset


_MD_SPECIAL = set("\\`*_[]<>#~|")


def _md_escape(text) -> str:
    return "".join("\\" + ch if ch in _MD_SPECIAL else ch for ch in str(text))


def hits_to_markdown(hits, req) -> str:
    """標準検索の結果（hits は全件）を Markdown にする。各 hit は chatsearch: リンク。"""
    head = f"**{tr('chat.search.result.count', n=len(hits))}** — {_md_escape(scope_label(req))}"
    if req.include_archived:
        head += f" / {tr('chat.search.dialog.archived')}"
    lines = [head, ""]
    if not hits:
        lines.append(tr("chat.search.result.none"))
        return "\n".join(lines)
    for i, h in enumerate(hits[:_MAX_MD_HITS], 1):
        name = _md_escape(core._collapse(h.title))
        if req.scope == "all" and h.dataset is not None:
            name = f"{_md_escape(h.dataset)} / {name}"
        if h.archived:
            name += _md_escape(tr("chat.search.result.archived_mark"))
        role = tr("chat.role.user") if h.role == "user" else tr("chat.role.assistant")
        when = core.format_ts(h.updated)
        link = core.make_search_link(h.session_id, h.msg_index, req.query)
        lines.append(f"{i}. [{name}]({link}) · {role} · {when} — {_md_escape(h.snippet)}")
    if len(hits) > _MAX_MD_HITS:
        lines += ["", tr("chat.search.result.more", n=len(hits) - _MAX_MD_HITS)]
    return "\n".join(lines)


# ----- history -----


def history_work_dir(ds, *, create: bool) -> Path | None:
    if ds is None:
        return None
    try:
        import dataset_config
        return dataset_config.get_work_dir(ds, create=create)
    except Exception:
        return None


def record_search(req, ctx, *, dataset, n_hits, session_id) -> bool:
    if req.scope == "unbound" or dataset is None:
        return False
    wd = history_work_dir(dataset, create=True)
    if wd is None:
        return False
    datasets = [req.dataset] if req.scope == "dataset" else list(ctx.open_datasets or [])
    try:
        entry = history.new_entry(req, datasets=datasets, n_hits=n_hits, session_id=session_id)
    except ValueError:
        return False
    return history.append_history(wd, entry)


# ----- ui_prefs -----


def _read_pref(key, default):
    try:
        from llm_bridge.paths import read_ui_pref
        return read_ui_pref(key, default)
    except Exception:
        return default


def _write_pref(key, value) -> None:
    try:
        from llm_bridge.paths import update_ui_pref
        update_ui_pref(key, value)
    except Exception:
        pass


def _load_bool(key) -> bool:
    v = _read_pref(key, False)
    return v if isinstance(v, bool) else False


def load_scope_pref() -> str:
    v = _read_pref("chat_search_scope", "dataset")
    return v if v in ("dataset", "all") else "dataset"


def load_archived_pref() -> bool:
    return _load_bool("chat_search_archived")


def load_ai_pref() -> bool:
    return _load_bool("chat_search_ai")


def load_history_open_pref() -> bool:
    return _load_bool("chat_search_history_open")


# ----- dialog -----


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


class ChatSearchDialog(QDialog):
    def __init__(self, chat_widget, parent=None):
        super().__init__(parent)
        self._chat = chat_widget
        self._jump_sid: str | None = None
        self._history_rows: list[tuple[str, dict]] = []
        self._applying_entry = False
        self.setWindowTitle(tr("chat.search.dialog.title"))
        self.setSizeGripEnabled(True)

        ctx = chat_widget.search_context()
        self._current = ctx.current_dataset
        open_ds = list(ctx.open_datasets or [])
        if self._current is not None and self._current not in open_ds:
            open_ds.insert(0, self._current)
        self._open = open_ds

        layout = QVBoxLayout(self)

        self._active_label = QLabel("")
        f = self._active_label.font()
        f.setBold(True)
        self._active_label.setFont(f)
        if self._current is not None:
            self._active_label.setText(
                tr("chat.search.dialog.active_dataset", dataset=self._current))
        elif not open_ds:
            self._active_label.setText(tr("chat.search.dialog.no_dataset"))
        else:
            self._active_label.hide()
        layout.addWidget(self._active_label)

        # ----- query pages -----
        self._mode_stack = QStackedWidget()
        page0 = QWidget()
        f0 = QFormLayout(page0)
        f0.setContentsMargins(0, 0, 0, 0)
        self._query_edit = QLineEdit()
        f0.addRow(tr("chat.search.dialog.query"), self._query_edit)
        page1 = QWidget()
        f1 = QFormLayout(page1)
        f1.setContentsMargins(0, 0, 0, 0)
        self._ai_query_edit = QPlainTextEdit()
        self._ai_query_edit.setPlaceholderText(tr("chat.search.dialog.ai_query_placeholder"))
        self._ai_query_edit.setTabChangesFocus(True)
        lh = self._ai_query_edit.fontMetrics().lineSpacing()
        self._ai_query_edit.setFixedHeight(lh * 5 + 12)
        self._ai_query_edit.installEventFilter(self)
        f1.addRow(tr("chat.search.dialog.ai_query"), self._ai_query_edit)
        self._hint_edit = QLineEdit()
        self._hint_edit.setPlaceholderText(tr("chat.search.dialog.hint_placeholder"))
        f1.addRow(tr("chat.search.dialog.hint"), self._hint_edit)
        self._mode_stack.addWidget(page0)
        self._mode_stack.addWidget(page1)
        layout.addWidget(self._mode_stack)

        # ----- options -----
        form = QFormLayout()
        self._scope_combo = QComboBox()
        if open_ds:
            for ds in open_ds:
                self._scope_combo.addItem(tr("chat.search.scope.dataset", dataset=ds),
                                          ("dataset", ds))
            self._scope_combo.addItem(
                tr("chat.search.scope.all_named", datasets=", ".join(open_ds)), ("all", None))
        else:
            self._scope_combo.addItem(tr("chat.search.scope.none"), ("none", None))
        if open_ds:
            if load_scope_pref() == "all":
                self._scope_combo.setCurrentIndex(self._scope_combo.count() - 1)
            else:
                front = self._current if self._current is not None else open_ds[0]
                self._scope_combo.setCurrentIndex(open_ds.index(front))
        form.addRow(tr("chat.search.dialog.scope"), self._scope_combo)
        self._switch_note = QLabel("")
        self._switch_note.setWordWrap(True)
        form.addRow("", self._switch_note)
        self._archived_check = QCheckBox(tr("chat.search.dialog.archived"))
        self._archived_check.setChecked(load_archived_pref())
        form.addRow("", self._archived_check)
        self._ai_check = QCheckBox(tr("chat.search.dialog.ai"))
        self._ai_check.setToolTip(tr("chat.search.dialog.ai_hint"))
        self._ai_check.setChecked(load_ai_pref())
        form.addRow("", self._ai_check)
        self._engine_label = QLabel(ai_engine_label_text())
        form.addRow("", self._engine_label)
        layout.addLayout(form)
        self._mode_stack.setCurrentIndex(1 if self._ai_check.isChecked() else 0)

        # ----- history (collapsed by default) -----
        self._history_toggle = QToolButton()
        self._history_toggle.setCheckable(True)
        self._history_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._history_toggle.setAutoRaise(True)
        layout.addWidget(self._history_toggle)
        self._history_panel = QWidget()
        hl = QVBoxLayout(self._history_panel)
        hl.setContentsMargins(0, 0, 0, 0)
        self._history_tree = QTreeWidget()
        self._history_tree.setColumnCount(_HISTORY_COLS)
        self._history_tree.setHeaderLabels([
            tr("chat.search.history.col.time"), tr("chat.search.history.col.mode"),
            tr("chat.search.history.col.query"), tr("chat.search.history.col.scope"),
            tr("chat.search.history.col.hits"), tr("chat.search.history.col.dataset"),
        ])
        self._history_tree.setRootIsDecorated(False)
        self._history_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._history_tree.setMinimumHeight(
            self._history_tree.fontMetrics().lineSpacing() * 8 + 24)
        hl.addWidget(self._history_tree)
        self._history_delete_btn = QPushButton(tr("chat.search.history.delete"))
        self._history_delete_btn.setEnabled(False)
        hl.addWidget(self._history_delete_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        self._history_note = QLabel("")
        self._history_note.setWordWrap(True)
        self._history_note.hide()
        hl.addWidget(self._history_note)
        layout.addWidget(self._history_panel)
        opened = load_history_open_pref()
        self._history_toggle.setChecked(opened)     # connect より前（初期化で保存しない）
        self._history_panel.setVisible(opened)
        self._history_toggle.setArrowType(
            Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow)

        # ----- buttons -----
        self._buttons = QDialogButtonBox()
        self._search_btn = self._buttons.addButton(
            tr("chat.search.btn.search"), QDialogButtonBox.ButtonRole.AcceptRole)
        self._search_btn.setDefault(True)
        self._buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        # ----- signals -----
        self._query_edit.textChanged.connect(self._update_search_enabled)
        self._ai_query_edit.textChanged.connect(self._update_search_enabled)
        self._ai_check.toggled.connect(self._on_ai_toggled)
        self._scope_combo.currentIndexChanged.connect(self._on_scope_changed)
        self._history_toggle.toggled.connect(self._on_history_toggled)
        self._history_tree.currentItemChanged.connect(self._on_history_current)
        self._history_tree.itemDoubleClicked.connect(self._on_history_double)
        self._history_delete_btn.clicked.connect(self._on_history_delete)

        self._update_switch_note()
        self._reload_history()
        self._update_search_enabled()
        QTimer.singleShot(0, self._focus_main_input)

    # ----- helpers -----

    def _focus_main_input(self) -> None:
        w = self._ai_query_edit if self._ai_check.isChecked() else self._query_edit
        w.setFocus()

    def _main_text(self) -> str:
        if self._ai_check.isChecked():
            return self._ai_query_edit.toPlainText().strip()
        return self._query_edit.text().strip()

    def _update_search_enabled(self, *_) -> None:
        self._search_btn.setEnabled(bool(self._main_text()))

    def _scope_data(self) -> tuple:
        d = self._scope_combo.currentData()
        return d if isinstance(d, tuple) else ("none", None)

    def _scope_index(self, data: tuple) -> int:
        for i in range(self._scope_combo.count()):
            if self._scope_combo.itemData(i) == data:
                return i
        return -1

    def _update_switch_note(self) -> None:
        kind, ds = self._scope_data()
        target = None
        if kind == "dataset" and ds != self._current:
            target = ds
        elif kind == "all" and self._current is None and self._open:
            target = self._open[0]
        if target is not None:
            self._switch_note.setText(tr("chat.search.dialog.will_switch", dataset=target))
            self._switch_note.show()
        else:
            self._switch_note.setText("")
            self._switch_note.hide()

    def _set_note(self, text: str) -> None:
        self._history_note.setText(text)
        self._history_note.setVisible(bool(text))

    def _on_ai_toggled(self, on: bool) -> None:
        self._mode_stack.setCurrentIndex(1 if on else 0)
        if on:
            t = self._query_edit.text().strip()
            if t:
                self._ai_query_edit.setPlainText(t)
        else:
            body = self._ai_query_edit.toPlainText()
            if body.strip():
                self._query_edit.setText(" ".join(body.split()))
        self._update_search_enabled()

    def _on_scope_changed(self, *_) -> None:
        self._update_switch_note()
        if not self._applying_entry:
            self._reload_history()

    # ----- history -----

    def _history_datasets(self) -> list[str]:
        kind, ds = self._scope_data()
        if kind == "dataset":
            return [ds]
        if kind == "all":
            return list(self._open)
        return []

    def _reload_history(self) -> None:
        kind, _ = self._scope_data()
        rows: list[tuple[str, dict]] = []
        bad = False
        for ds in self._history_datasets():
            wd = history_work_dir(ds, create=False)
            if wd is None:
                bad = True
                continue
            entries, status = history.load_history(wd)
            if status == "unreadable":
                bad = True
            rows.extend((ds, e) for e in entries)
        rows.sort(key=lambda r: -r[1]["ts"])
        self._history_rows = rows
        self._history_tree.clear()
        for i, (ds, e) in enumerate(rows):
            item = QTreeWidgetItem([
                core.format_ts(e["ts"]),
                tr("chat.search.mode.ai") if e["mode"] == "ai" else tr("chat.search.mode.text"),
                _first_line(e["query"]),
                tr("chat.search.scope.all_short") if e["scope"] == "all" else e["datasets"][0],
                str(e["n_hits"]) if isinstance(e["n_hits"], int) else "—",
                ds,
            ])
            tip = e["query"]
            if e["hint"]:
                tip += "\n" + tr("chat.search.dialog.hint") + " " + e["hint"]
            item.setToolTip(2, tip)
            item.setData(0, Qt.ItemDataRole.UserRole, i)
            self._history_tree.addTopLevelItem(item)
        self._history_tree.setColumnHidden(_COL_DATASET, kind != "all")
        self._history_toggle.setText(tr("chat.search.history.title_n", n=len(rows)))
        self._history_delete_btn.setEnabled(False)
        self._set_note(tr("chat.search.history.unreadable") if bad else "")

    def _row_of(self, item) -> tuple[str, dict] | None:
        if item is None:
            return None
        i = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(i, int) or not 0 <= i < len(self._history_rows):
            return None
        return self._history_rows[i]

    def _on_history_toggled(self, on: bool) -> None:
        self._history_panel.setVisible(on)
        self._history_toggle.setArrowType(
            Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        _write_pref("chat_search_history_open", bool(on))
        self.adjustSize()

    def _on_history_current(self, item, _prev=None) -> None:
        row = self._row_of(item)
        self._history_delete_btn.setEnabled(row is not None)
        if row is not None:
            self._apply_entry(*row)

    def _apply_entry(self, ds: str, entry: dict) -> None:
        self._applying_entry = True
        try:
            self._ai_check.setChecked(entry["mode"] == "ai")
            if entry["mode"] == "ai":
                self._ai_query_edit.setPlainText(entry["query"])
                self._hint_edit.setText(entry["hint"])
            else:
                self._query_edit.setText(entry["query"])
            if entry["scope"] == "all":
                i = self._scope_index(("all", None))
                if i >= 0:
                    self._scope_combo.setCurrentIndex(i)
            elif entry["datasets"]:
                target = entry["datasets"][0]
                i = self._scope_index(("dataset", target))
                if i >= 0:
                    self._scope_combo.setCurrentIndex(i)
                else:
                    self._set_note(tr("chat.search.history.dataset_not_open", dataset=target))
            self._archived_check.setChecked(entry["include_archived"])
        finally:
            self._applying_entry = False
        self._update_search_enabled()

    def _on_history_double(self, item, _col=0) -> None:
        row = self._row_of(item)
        if row is None:
            return
        sess = core.resolve_session(self._chat.search_context(), row[1]["session_id"])
        if sess is None:
            self._set_note(tr("chat.search.history.no_results_tab"))
            return
        self._jump_sid = sess.id
        self.accept()

    def _on_history_delete(self) -> None:
        row = self._row_of(self._history_tree.currentItem())
        if row is None:
            return
        ds, entry = row
        wd = history_work_dir(ds, create=False)
        ok = wd is not None and history.delete_history_entry(wd, entry["id"])
        self._reload_history()
        if not ok:
            self._set_note(tr("chat.search.history.write_failed"))

    # ----- keys / result -----

    def eventFilter(self, obj, ev) -> bool:
        if obj is self._ai_query_edit and ev.type() == QEvent.Type.KeyPress \
                and ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) \
                and (ev.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.accept()
            return True
        return super().eventFilter(obj, ev)

    def accept(self) -> None:
        if self._jump_sid is None:
            if not self._main_text():
                return
            kind, _ = self._scope_data()
            _write_pref("chat_search_scope", "all" if kind == "all" else "dataset")
            _write_pref("chat_search_archived", self._archived_check.isChecked())
            _write_pref("chat_search_ai", self._ai_check.isChecked())
        super().accept()

    def request(self) -> SearchRequest | None:
        if self._jump_sid is not None:
            return None
        ai = self._ai_check.isChecked()
        if ai:
            query = self._ai_query_edit.toPlainText().strip()
            hint = self._hint_edit.text().strip()
        else:
            query = self._query_edit.text().strip()
            hint = ""
        kind, ds = self._scope_data()
        if kind == "dataset":
            scope, dataset = "dataset", ds
        elif kind == "all":
            scope, dataset = "all", None
        else:
            scope, dataset = "unbound", None
        return SearchRequest(query=query, scope=scope, dataset=dataset,
                             include_archived=self._archived_check.isChecked(),
                             ai=ai, hint=hint)

    def jump_session_id(self) -> str | None:
        return self._jump_sid
