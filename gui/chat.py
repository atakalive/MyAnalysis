"""Chat widget for LLM dialogue. Mounted in ToolWindow's chat dock."""
from __future__ import annotations

import html

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QKeyEvent, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QPlainTextEdit, QPushButton, QTextBrowser,
    QVBoxLayout, QWidget,
)

from gui.llm import LLMBackend, Message


class _StreamWorker(QThread):
    chunk = Signal(str)
    done = Signal()
    failed = Signal(str)

    def __init__(self, backend: LLMBackend, messages: list[Message], parent=None):
        super().__init__(parent)
        self._backend = backend
        self._messages = messages

    def run(self) -> None:
        try:
            for piece in self._backend.stream(self._messages):
                if self.isInterruptionRequested():
                    self.done.emit()
                    return
                self.chunk.emit(piece)
        except Exception as e:
            self.failed.emit(repr(e))
            return
        self.done.emit()


class ChatWidget(QWidget):
    def __init__(self, backend: LLMBackend, parent=None):
        super().__init__(parent)
        self._backend = backend
        self._history: list[Message] = []
        self._worker: _StreamWorker | None = None
        self._assistant_buffer = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._log = QTextBrowser()
        self._log.setOpenExternalLinks(True)
        layout.addWidget(self._log, stretch=1)

        self._input = QPlainTextEdit()
        self._input.setPlaceholderText(
            "Message... (Enter to send, Shift+Enter for newline)"
        )
        self._input.setFixedHeight(80)
        self._input.installEventFilter(self)
        layout.addWidget(self._input)

        row = QHBoxLayout()
        row.addStretch(1)
        self._send_btn = QPushButton("Send")
        self._send_btn.clicked.connect(self._on_send)
        row.addWidget(self._send_btn)
        layout.addLayout(row)

        self._append_system_line(f"backend: {backend.name} / model: {backend.model}")

        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._shutdown_worker)

    def eventFilter(self, obj, ev) -> bool:
        if obj is self._input and isinstance(ev, QKeyEvent) \
                and ev.type() == QKeyEvent.Type.KeyPress \
                and ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) \
                and not (ev.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self._on_send()
            return True
        return super().eventFilter(obj, ev)

    # ----- send / receive -----

    def _on_send(self) -> None:
        if self._worker is not None:
            return
        text = self._input.toPlainText()
        if not text.strip():
            return
        self._input.clear()
        self._history.append(Message(role="user", content=text))
        self._append_block("user", text)
        self._assistant_buffer = ""
        self._append_block("assistant", "")
        self._send_btn.setEnabled(False)
        self._worker = _StreamWorker(self._backend, list(self._history), self)
        self._worker.chunk.connect(self._on_chunk)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_chunk(self, piece: str) -> None:
        self._assistant_buffer += piece
        cursor = QTextCursor(self._log.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.setCharFormat(QTextCharFormat())
        cursor.insertText(piece)
        self._scroll_to_bottom()

    def _on_done(self) -> None:
        self._history.append(Message(role="assistant", content=self._assistant_buffer))
        self._finalize()

    def _on_failed(self, msg: str) -> None:
        cursor = QTextCursor(self._log.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.setCharFormat(QTextCharFormat())
        cursor.insertText(f"\n\n[error: {msg}]")
        self._scroll_to_bottom()
        self._history.append(Message(role="assistant", content=self._assistant_buffer))
        self._finalize()

    def _finalize(self) -> None:
        self._send_btn.setEnabled(True)
        if self._worker is not None:
            self._worker.deleteLater()
        self._worker = None

    def _shutdown_worker(self) -> None:
        """アプリ終了時にストリーミングスレッドを停止する。"""
        if self._worker is not None and self._worker.isRunning():
            self._worker.requestInterruption()
            if not self._worker.wait(5000):
                self._worker.terminate()
                self._worker.wait(1000)

    # ----- display helpers -----
    # 追記専用の QTextCursor 操作。ストリーミング中は末尾への insertText のみ
    # 行うため O(n)。既存ブロックの再描画は発生しない。

    def _scroll_to_bottom(self) -> None:
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _append_system_line(self, text: str) -> None:
        self._log.append(
            f'<span style="color:#888888;font-style:italic">'
            f'{html.escape(text)}</span>'
        )
        self._scroll_to_bottom()

    def _append_block(self, role: str, text: str) -> None:
        """role ヘッダ + 本文を末尾に追加する。"""
        doc = self._log.document()
        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if doc.characterCount() > 1:
            cursor.insertBlock()
        color_map = {"user": "#6ec1e4", "assistant": "#a8d08d"}
        color = color_map.get(role, "#cccccc")
        cursor.insertHtml(f'<b style="color:{color}">{html.escape(role)}</b>')
        cursor.insertBlock()
        cursor.setCharFormat(QTextCharFormat())
        if text:
            cursor.insertText(text)
        self._scroll_to_bottom()
