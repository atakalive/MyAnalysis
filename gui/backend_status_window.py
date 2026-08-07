"""バックエンド状況ウィンドウ — 対応 LLM ドライバの導入状況を棚卸しし、
CLI をなるべく使わずに導入まで進める（設定 → バックエンドの状況…）。

非モーダル（開いたまま本体を触れる）。npm インストールは 20 秒ほどかかるし、ログインは
別ウィンドウの端末で操作してもらう間ずっとこの窓が開いている必要があるため。

**課金しない**: 開く／再確認で走るのは `llm_backend.preflight` のローカル判定だけで、
LLM 推論は 1 回も走らない。実際に 1 ターン投げる疎通確認は**ユーザーが行ごとのボタンを
押したときだけ**。meeting_share のような定期 QTimer 更新は置かない（毎回プロセスを起動
するため）。
"""

from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QGridLayout, QGroupBox, QLabel, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from common.i18n import tr
from common.proc import no_window_kwargs
from llm_backend import build_backend, preflight
from llm_backend.engines import ENGINES, engine_by_id, engine_label, session_settings
from llm_backend.ping import ping_backend

_MARK = {"ok": "✓", "missing": "✗", "unknown": "?", "n/a": "—"}


class _ProbeWorker(QThread):
    """全ドライバの状態を並列に調べ、確定した行から返す。

    QThread を 6 本立てず、1 本の中で ThreadPoolExecutor に投げる（中身は subprocess 待ち
    なので GIL は解放される）。逐次だと 8 秒超で `pi --list-models` の 4.6 秒が支配的。

    `generation` は stale 結果を捨てるため（再確認を連打されても古い結果で上書きしない）。
    meeting/relay.py の _TunnelStarter と同じ考え方。
    """

    row = Signal(int, object)       # (generation, EngineStatus)
    finished_all = Signal(int)

    def __init__(self, generation: int, engine_ids: list[str], parent=None):
        super().__init__(parent)
        self._generation = generation
        self._ids = engine_ids

    def run(self) -> None:
        try:
            with ThreadPoolExecutor(max_workers=min(8, len(self._ids) or 1)) as ex:
                for status in ex.map(preflight.check_engine, self._ids):
                    if self.isInterruptionRequested():
                        return
                    self.row.emit(self._generation, status)
        except Exception:               # never raise into Qt
            pass
        self.finished_all.emit(self._generation)


class _CmdWorker(QThread):
    """外部コマンドを実行し、出力を 1 行ずつ流す（npm install 用）。"""

    line = Signal(int, str)
    done = Signal(int, int)         # (generation, returncode)

    def __init__(self, generation: int, cmd: list[str], parent=None):
        super().__init__(parent)
        self._generation = generation
        self._cmd = cmd
        self._proc: subprocess.Popen | None = None

    def run(self) -> None:
        rc = -1
        try:
            self._proc = subprocess.Popen(
                preflight._wrap(self._cmd),      # win32: npm.cmd は cmd.exe /c 経由
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",               # cp932 で化けさせない
                errors="replace",
                **no_window_kwargs(),
            )
            for ln in self._proc.stdout:
                if self.isInterruptionRequested():
                    break
                self.line.emit(self._generation, ln.rstrip())
            rc = self._proc.wait()
        except Exception as e:                  # never raise into Qt
            self.line.emit(self._generation, f"[error] {e}")
        self.done.emit(self._generation, rc)

    def kill(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.kill()
            except Exception:
                pass


class _PingWorker(QThread):
    result = Signal(int, object)

    def __init__(self, generation: int, backend, parent=None):
        super().__init__(parent)
        self._generation = generation
        self.backend = backend

    def run(self) -> None:
        self.result.emit(self._generation, ping_backend(self.backend))


class BackendStatusWindow(QWidget):
    def __init__(self, window, parent=None):
        super().__init__(parent)
        self._window = window
        self._generation = 0
        self._workers: list[QThread] = []
        self._probe: _ProbeWorker | None = None
        self._cmd: _CmdWorker | None = None
        self._ping: _PingWorker | None = None
        self._ping_backend = None
        self._rows: dict[str, dict] = {}
        self._status: dict[str, preflight.EngineStatus] = {}

        self.setWindowTitle(tr("backend.status.title"))
        self.resize(760, 520)
        root = QVBoxLayout(self)

        head = QGroupBox(tr("backend.status.drivers"), self)
        self._grid = QGridLayout(head)
        for col, key in enumerate((
            "backend.status.col.driver", "backend.status.col.prereq",
            "backend.status.col.install", "backend.status.col.auth",
            "backend.status.col.action",
        )):
            self._grid.addWidget(QLabel(tr(key)), 0, col)
        for i, e in enumerate(ENGINES, start=1):
            self._build_row(i, e.id)
        root.addWidget(head)

        self._refresh_btn = QPushButton(tr("backend.status.refresh"), self)
        self._refresh_btn.clicked.connect(self.refresh)
        root.addWidget(self._refresh_btn)

        self._log = QPlainTextEdit(self)
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(500)
        root.addWidget(self._log, stretch=1)

        if hasattr(window, "register_retranslate_hook"):
            window.register_retranslate_hook(self.retranslate)
        self.refresh()

    # ----- rows -----

    def _build_row(self, r: int, engine_id: str) -> None:
        eng = engine_by_id(engine_id)
        name = QLabel(engine_label(eng) if eng else engine_id)
        prereq, install, auth = QLabel("…"), QLabel("…"), QLabel("…")
        act = QPushButton("", self)
        act.setVisible(False)
        act.clicked.connect(lambda _c=False, eid=engine_id: self._on_action(eid))
        login = QPushButton(tr("backend.status.btn.login"), self)
        login.setVisible(False)
        login.clicked.connect(lambda _c=False, eid=engine_id: self._on_login(eid))
        ping = QPushButton(tr("backend.status.btn.ping"), self)
        ping.setVisible(False)
        ping.clicked.connect(lambda _c=False, eid=engine_id: self._on_ping(eid))
        note = QLabel("")
        note.setWordWrap(True)
        note.setStyleSheet("color:#888;font-size:11px")
        for col, w in enumerate((name, prereq, install, auth)):
            self._grid.addWidget(w, r, col)
        self._grid.addWidget(act, r, 4)
        self._grid.addWidget(login, r, 5)
        self._grid.addWidget(ping, r, 6)
        self._grid.addWidget(note, r, 7)
        self._rows[engine_id] = {
            "prereq": prereq, "install": install, "auth": auth,
            "action": act, "login": login, "ping": ping, "note": note,
        }

    @staticmethod
    def _cell(state: str, text: str) -> str:
        mark = _MARK.get(state, "")
        return f"{mark} {text}".strip() if text else mark

    def _paint(self, s: preflight.EngineStatus) -> None:
        row = self._rows.get(s.engine_id)
        if row is None:
            return
        self._status[s.engine_id] = s
        # preflight は i18n を知らない（CLI からも使うため）。文はここで組み立てる。
        notes = " / ".join(tr(k, **p) for k, p in getattr(s, "notes", ()))
        if s.note_key:
            notes = " / ".join(x for x in (notes, tr(s.note_key)) if x)
        row["prereq"].setText(self._cell(s.prereq_state, s.prereq_detail))
        row["install"].setText(self._cell(s.binary_state, s.version or ""))
        auth_text = ", ".join(s.authed_providers) or s.auth_detail
        row["auth"].setText(self._cell(s.auth_state, auth_text))
        row["note"].setText(notes)
        row["note"].setToolTip(notes)

        # インストール/更新は同じ `npm i -g <pkg>`。ラベルだけ状態で変える。
        # preflight 側が unknown / 前提不足のとき install を None にしているので、
        # 「node が無いのにインストールを押せる」「遅いだけなのに再インストールを勧める」
        # は構造的に起きない。
        act = row["action"]
        if s.install:
            act.setText(tr("backend.status.btn.install") if s.binary_state == "missing"
                        else tr("backend.status.btn.update"))
            act.setVisible(True)
        else:
            act.setVisible(False)
        # ログインは認証済みでも出す（アカウント切替・再ログインは正当な操作）。
        row["login"].setVisible(bool(s.login))
        # 疎通確認は「入っている」行にだけ。押したときだけ課金される。
        row["ping"].setVisible(s.binary_state in ("ok", "n/a"))

    # ----- probe -----

    def refresh(self) -> None:
        """状態を再判定する。開いた直後と「再確認」押下のときだけ呼ばれる。"""
        self._stop_probe()
        self._generation += 1
        gen = self._generation
        for row in self._rows.values():
            row["action"].setVisible(False)
        w = _ProbeWorker(gen, [e.id for e in ENGINES], QApplication.instance())
        self._probe = w
        self._workers.append(w)
        w.row.connect(self._on_row)
        w.finished.connect(w.deleteLater)
        w.finished.connect(lambda _w=w: self._forget(_w))
        self._refresh_btn.setEnabled(False)
        w.finished_all.connect(self._on_probe_done)
        w.start()

    def _on_row(self, generation: int, status) -> None:
        if generation != self._generation:
            return                      # stale: 連打された古いワーカーの結果
        self._paint(status)

    def _on_probe_done(self, generation: int) -> None:
        if generation == self._generation:
            self._refresh_btn.setEnabled(True)

    # ----- actions -----

    def _on_action(self, engine_id: str) -> None:
        """インストール / 更新。コマンドはどちらも同じ ``npm i -g <pkg>``。"""
        s = self._status.get(engine_id)
        if s is not None and s.install:
            self._run_install(list(s.install))

    def _on_login(self, engine_id: str) -> None:
        s = self._status.get(engine_id)
        if s is not None and s.login:
            self._launch_login(list(s.login))

    def _run_install(self, cmd: list[str]) -> None:
        if self._running(self._cmd):
            return
        self._append(f"$ {' '.join(cmd)}")
        w = _CmdWorker(self._generation, cmd, QApplication.instance())
        self._cmd = w
        self._workers.append(w)
        w.line.connect(lambda _g, ln: self._append(ln))
        w.done.connect(self._on_install_done)
        w.finished.connect(w.deleteLater)
        w.finished.connect(lambda _w=w: self._forget(_w))
        w.start()

    def _on_install_done(self, _generation: int, rc: int) -> None:
        self._append(tr("backend.status.install_done", code=rc))
        # 新しく入ったバイナリを拾い直す。PATH は起動時のものなので、npm の global bin が
        # PATH に無い構成では missing のままになる — その場合は再起動が要る。
        self.refresh()

    def _launch_login(self, cmd: list[str]) -> None:
        """端末ウィンドウを起こしてログインしてもらう。

        pi の /login は対話 TUI で headless にできないため、ここだけは端末が要る。
        非 Windows では端末エミュレータを推測せず、コマンドを表示するに留める。
        """
        if sys.platform != "win32":
            self._append(tr("backend.status.login_manual", cmd=" ".join(cmd)))
            return
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        try:
            subprocess.Popen(preflight._wrap(cmd), creationflags=flags)
        except Exception as e:
            self._append(f"[error] {e}")
            return
        self._append(tr("backend.status.login_started", cmd=" ".join(cmd)))

    def _on_ping(self, engine_id: str) -> None:
        """実際に 1 ターン投げる。**ユーザーが押したときだけ**走る（課金される）。"""
        if self._running(self._ping):
            return
        eng = engine_by_id(engine_id)
        if eng is None:
            return
        try:
            backend = build_backend(
                eng.backend_key, session_settings(eng, "", "")
            )
        except Exception as e:
            self._append(f"[error] {e}")
            return
        self._append(tr("backend.status.ping_started", name=engine_label(eng)))
        w = _PingWorker(self._generation, backend, QApplication.instance())
        self._ping, self._ping_backend = w, backend
        self._workers.append(w)
        w.result.connect(self._on_ping_result)
        w.finished.connect(w.deleteLater)
        w.finished.connect(lambda _w=w: self._forget(_w))
        self._ping_timeout = QTimer(self)
        self._ping_timeout.setSingleShot(True)
        self._ping_timeout.timeout.connect(lambda b=backend: self._ping_expire(b))
        self._ping_timeout.start(60000)
        w.start()

    def _ping_expire(self, backend) -> None:
        if hasattr(backend, "cancel"):
            try:
                backend.cancel()
            except Exception:
                pass
        t = QTimer(self)
        t.setSingleShot(True)
        t.timeout.connect(lambda: backend.kill() if hasattr(backend, "kill") else None)
        t.start(2000)
        self._ping_kill = t

    def _on_ping_result(self, _generation: int, result) -> None:
        self._ping, self._ping_backend = None, None
        if result.ok:
            self._append(tr("backend.status.ping_ok",
                            elapsed=f"{result.elapsed:.1f}", text=result.text))
        else:
            self._append(tr("backend.status.ping_fail", error=result.error))

    # ----- lifecycle -----

    def _append(self, text: str) -> None:
        if text:
            self._log.appendPlainText(text)

    def _forget(self, worker) -> None:
        try:
            self._workers.remove(worker)
        except ValueError:
            pass
        # 役割スロットも手放す。finished→deleteLater で C++ 側が消えた後に
        # isRunning() を呼ぶと RuntimeError が closeEvent から漏れる。
        for attr in ("_probe", "_cmd", "_ping"):
            if getattr(self, attr, None) is worker:
                setattr(self, attr, None)

    @staticmethod
    def _running(worker) -> bool:
        """deleteLater 済みの wrapper に触れても落ちない isRunning()。"""
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:            # Internal C++ object already deleted
            return False

    def _stop_probe(self) -> None:
        w = self._probe
        if self._running(w):
            w.requestInterruption()
            w.wait(2000)
        self._probe = None

    def _stop_all(self) -> None:
        """cancel → wait → kill。backend_selector_dialog の作法に倣う。"""
        self._stop_probe()
        if self._running(self._cmd):
            self._cmd.requestInterruption()
            self._cmd.kill()
            self._cmd.wait(2000)
        self._cmd = None
        if self._running(self._ping):
            b = self._ping_backend
            if b is not None and hasattr(b, "cancel"):
                try:
                    b.cancel()
                except Exception:
                    pass
            self._ping.requestInterruption()
            self._ping.wait(2000)
            if self._running(self._ping) and b is not None and hasattr(b, "kill"):
                try:
                    b.kill()
                except Exception:
                    pass
                self._ping.wait(2000)
        self._ping = self._ping_backend = None

    def closeEvent(self, event) -> None:
        self._stop_all()
        super().closeEvent(event)

    def retranslate(self) -> None:
        self.setWindowTitle(tr("backend.status.title"))
        self._refresh_btn.setText(tr("backend.status.refresh"))
        for eid, row in self._rows.items():
            row["ping"].setText(tr("backend.status.btn.ping"))
            row["login"].setText(tr("backend.status.btn.login"))
            s = self._status.get(eid)
            if s is not None:
                self._paint(s)
