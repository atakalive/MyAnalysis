"""Meeting share window (Issue #42).

Non-modal window to start/stop a meeting, show the invite token, pick which chat
sessions and analysis tabs are shared, and watch participants + a live log.
Opened from View → ミーティング共有…. All user-facing strings go through `tr()`.
"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from common.i18n import tr

# Duration options (label suffix, seconds). Max 24h, server-clamped to [1h, 24h].
_TTL_OPTIONS = [("1h", 3600), ("3h", 10800), ("6h", 21600),
                ("12h", 43200), ("24h", 86400)]


class MeetingShareWindow(QWidget):
    def __init__(self, window, relay, parent=None):
        super().__init__(parent)
        self._window = window
        self._relay = relay
        self._token = ""
        self._token_revealed = False
        self._sess_sig = None       # signature of the rendered session rows
        self._tab_sig = None
        self._sess_boxes: dict[str, QCheckBox] = {}
        # Keyed by (dataset-key, tab-name) — all open DSs are shareable (Issue #78),
        # so bare names would collide across datasets.
        self._tab_boxes: dict[tuple[str, str], QCheckBox] = {}
        self.setWindowTitle(tr("meeting.share.title"))
        self.resize(520, 720)

        root = QVBoxLayout(self)

        # ----- status row -----
        status_row = QHBoxLayout()
        self._state_label = QLabel(tr("meeting.state.idle"))
        self._channel_label = QLabel("")
        self._stop_btn = QPushButton(tr("meeting.btn.stop"))
        self._stop_btn.clicked.connect(self._on_stop)
        status_row.addWidget(self._state_label)
        status_row.addWidget(self._channel_label)
        status_row.addStretch(1)
        status_row.addWidget(self._stop_btn)
        root.addLayout(status_row)

        # ----- relay / connection -----
        relay_row = QHBoxLayout()
        self._relay_label = QLabel("")
        self._conn_label = QLabel("")
        relay_row.addWidget(self._relay_label, stretch=1)
        relay_row.addWidget(self._conn_label)
        root.addLayout(relay_row)

        # ----- start controls -----
        start_row = QHBoxLayout()
        self._ttl_caption = QLabel(tr("meeting.label.ttl"))
        self._ttl_combo = QComboBox()
        for label, secs in _TTL_OPTIONS:
            self._ttl_combo.addItem(label, secs)
        self._ttl_combo.setCurrentIndex(1)   # default 3h
        self._name_caption = QLabel(tr("meeting.label.host_name"))
        self._name_edit = QLineEdit()
        self._name_edit.setText(relay.host_name)
        self._name_edit.textChanged.connect(self._on_name_changed)
        self._start_btn = QPushButton(tr("meeting.btn.start"))
        self._start_btn.clicked.connect(self._on_start)
        start_row.addWidget(self._ttl_caption)
        start_row.addWidget(self._ttl_combo)
        start_row.addWidget(self._name_caption)
        start_row.addWidget(self._name_edit, stretch=1)
        start_row.addWidget(self._start_btn)
        root.addLayout(start_row)

        # ----- LAN link controls -----
        lan_row = QHBoxLayout()
        self._lan_link_check = QCheckBox(tr("meeting.check.lan_link"))
        self._lan_host_caption = QLabel(tr("meeting.label.lan_host"))
        self._lan_host_edit = QLineEdit()
        self._lan_host_edit.setText(
            os.environ.get("RELAY_LAN_HOST", "") or self._primary_ip())
        lan_row.addWidget(self._lan_link_check)
        lan_row.addWidget(self._lan_host_caption)
        lan_row.addWidget(self._lan_host_edit, stretch=1)
        root.addLayout(lan_row)

        # ----- token -----
        self._token_caption = QLabel(tr("meeting.label.token"))
        root.addWidget(self._token_caption)
        token_row = QHBoxLayout()
        self._token_edit = QLineEdit()
        self._token_edit.setReadOnly(True)
        self._token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._reveal_btn = QPushButton(tr("meeting.btn.reveal"))
        self._reveal_btn.clicked.connect(self._on_reveal)
        self._copy_btn = QPushButton(tr("meeting.btn.copy"))
        self._copy_btn.clicked.connect(self._on_copy)
        self._copy_link_btn = QPushButton(tr("meeting.btn.copy_link"))
        self._copy_link_btn.clicked.connect(self._on_copy_link)
        self._copy_feedback = self._make_copied_timer(self._copy_btn, "meeting.btn.copy")
        self._copy_link_feedback = self._make_copied_timer(self._copy_link_btn, "meeting.btn.copy_link")
        # LAN link: full deep-link copy only (no bare LAN token).
        self._copy_lan_link_btn = QPushButton(tr("meeting.btn.copy_lan_link"))
        self._copy_lan_link_btn.clicked.connect(self._on_copy_lan_link)
        self._copy_lan_link_feedback = self._make_copied_timer(
            self._copy_lan_link_btn, "meeting.btn.copy_lan_link")
        token_row.addWidget(self._token_edit, stretch=1)
        token_row.addWidget(self._reveal_btn)
        root.addLayout(token_row)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self._copy_btn)
        btn_row.addWidget(self._copy_link_btn)
        btn_row.addWidget(self._copy_lan_link_btn)
        btn_row.addStretch(1)
        root.addLayout(btn_row)
        self._token_hint = QLabel(tr("meeting.token.hint"))
        self._token_hint.setWordWrap(True)
        self._token_hint.setStyleSheet("color:#888;font-size:11px")
        root.addWidget(self._token_hint)
        self._lan_https_hint = QLabel(tr("meeting.lan.https_hint"))
        self._lan_https_hint.setWordWrap(True)
        self._lan_https_hint.setStyleSheet("color:#888;font-size:11px")
        root.addWidget(self._lan_https_hint)
        self._remaining_label = QLabel("")
        root.addWidget(self._remaining_label)
        self._expiry_note = QLabel(tr("meeting.expiry.delayed"))
        self._expiry_note.setWordWrap(True)
        self._expiry_note.setStyleSheet("color:#888;font-size:11px")
        root.addWidget(self._expiry_note)

        # ----- sessions -----
        self._sess_group = QGroupBox(tr("meeting.section.sessions"))
        sess_outer = QVBoxLayout(self._sess_group)
        self._sess_select_all = QCheckBox(tr("meeting.btn.select_all"))
        self._sess_select_all.setTristate(True)
        self._sess_select_all.clicked.connect(self._on_select_all_sessions)
        sess_outer.addWidget(self._sess_select_all)
        self._auto_share_new = QCheckBox(tr("meeting.btn.auto_share_new_sessions"))
        self._auto_share_new.setChecked(relay.auto_share_new_sessions())
        self._auto_share_new.toggled.connect(self._on_auto_share_toggled)
        sess_outer.addWidget(self._auto_share_new)
        self._sess_scroll = QScrollArea()
        self._sess_scroll.setWidgetResizable(True)
        self._sess_inner = QWidget()
        self._sess_layout = QVBoxLayout(self._sess_inner)
        self._sess_scroll.setWidget(self._sess_inner)
        sess_outer.addWidget(self._sess_scroll)
        self._new_session_note = QLabel(tr("meeting.new_session_note"))
        self._new_session_note.setWordWrap(True)
        self._new_session_note.setStyleSheet("color:#d7ba7d;font-size:11px")
        self._new_session_note.setVisible(False)
        sess_outer.addWidget(self._new_session_note)
        root.addWidget(self._sess_group, stretch=1)

        # ----- tabs -----
        self._tab_group = QGroupBox(tr("meeting.section.tabs"))
        tab_outer = QVBoxLayout(self._tab_group)
        self._tab_select_all = QCheckBox(tr("meeting.btn.select_all"))
        self._tab_select_all.setTristate(True)
        self._tab_select_all.clicked.connect(self._on_select_all_tabs)
        tab_outer.addWidget(self._tab_select_all)
        self._tab_scroll = QScrollArea()
        self._tab_scroll.setWidgetResizable(True)
        self._tab_inner = QWidget()
        self._tab_layout = QVBoxLayout(self._tab_inner)
        self._tab_scroll.setWidget(self._tab_inner)
        tab_outer.addWidget(self._tab_scroll)
        root.addWidget(self._tab_group, stretch=1)

        # ----- participants -----
        self._part_group = QGroupBox(tr("meeting.section.participants"))
        part_outer = QVBoxLayout(self._part_group)
        self._part_label = QLabel("")
        self._part_label.setWordWrap(True)
        part_outer.addWidget(self._part_label)
        root.addWidget(self._part_group)

        # ----- log -----
        self._log_group = QGroupBox(tr("meeting.section.log"))
        log_outer = QVBoxLayout(self._log_group)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(500)
        log_outer.addWidget(self._log)
        root.addWidget(self._log_group, stretch=1)

        self._not_configured = QLabel(tr("meeting.not_configured"))
        self._not_configured.setWordWrap(True)
        self._not_configured.setStyleSheet("color:#f48771")
        root.addWidget(self._not_configured)

        # signals
        relay.channelStateChanged.connect(self._on_state)
        relay.participantsUpdated.connect(self._on_participants)
        relay.remoteMessageReceived.connect(self._on_remote_message)
        relay.tokenReady.connect(self._on_token_ready)

        # periodic refresh (remaining time + session/tab lists)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        if hasattr(window, "register_retranslate_hook"):
            window.register_retranslate_hook(self.retranslate)

        # Adopt a share already started via CLI/llm_bridge before this window
        # opened: tokenReady has already fired, so seed the current token here
        # (before _update_enabled → _update_state_label) so the window shows
        # "Sharing" + the token instead of a blank "starting" view.
        self._token = self._relay.current_token()
        if self._token:
            self._token_edit.setText(self._token)

        self._refresh_lists()
        self._update_enabled()

    # ---- start / stop ----

    def _on_start(self) -> None:
        if not self._relay.is_configured():
            return
        self._relay.set_host_name(self._name_edit.text())
        ttl = self._ttl_combo.currentData()
        lan = self._lan_link_check.isChecked()
        host_ip = self._lan_host_edit.text().strip()
        try:
            self._relay.meeting_start(int(ttl), lan=lan, host_ip=host_ip)
        except Exception as e:
            self._log_line(f"start failed: {e!r}")
            return
        # LAN が有効なら実際の ip:port をログ表示（固定ポート要求が typo 等で ephemeral
        # 化した場合に「要求ポートで無い」ことを可視化する。reviewer R2 P2-2）。
        lan_base = self._relay.lan_base_url()
        if lan_base:
            self._log_line(tr("meeting.lan.ready", url=lan_base))
        # Token arrives asynchronously via _on_token_ready (tunnel URL resolves
        # off-thread); LAN リンクは meeting_start 完了時点で final（_update_enabled が
        # relay.lan_link() を live 照会して LAN ボタンを有効化する）。
        self._update_state_label()
        self._update_enabled()

    def _on_token_ready(self, token: str) -> None:
        self._token = token
        self._token_edit.setText(token)
        self._sess_sig = None    # force a list rebuild against the new published set
        self._tab_sig = None
        self._log_line(tr("meeting.state.sharing"))
        self._update_state_label()
        self._refresh_lists()
        self._update_enabled()

    def _on_stop(self) -> None:
        self._relay.meeting_stop()
        self._token = ""
        self._token_edit.setText("")
        self._update_state_label()
        self._update_enabled()
        self._log_line(tr("meeting.state.idle"))

    # ---- token actions ----

    def _on_copy(self) -> None:
        if self._token:
            QApplication.clipboard().setText(self._token)
            self._show_copied(self._copy_btn, self._copy_feedback)

    def _share_url(self) -> str:
        base = self._relay.base_url()
        if not self._token or not base:
            return ""
        return base.rstrip("/") + "/#token=" + self._token

    def _on_copy_link(self) -> None:
        url = self._share_url()
        if url:
            QApplication.clipboard().setText(url)
            self._show_copied(self._copy_link_btn, self._copy_link_feedback)

    def _make_copied_timer(self, btn: QPushButton, restore_key: str) -> QTimer:
        t = QTimer(self)
        t.setSingleShot(True)
        t.setInterval(1000)
        t.timeout.connect(lambda: btn.setText(tr(restore_key)))
        return t

    def _show_copied(self, btn: QPushButton, timer: QTimer) -> None:
        btn.setText(tr("meeting.btn.copied"))
        timer.start()

    def _on_reveal(self) -> None:
        self._token_revealed = not self._token_revealed
        self._token_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if self._token_revealed
            else QLineEdit.EchoMode.Password
        )
        self._reveal_btn.setText(
            tr("meeting.btn.hide") if self._token_revealed else tr("meeting.btn.reveal")
        )

    def _lan_share_url(self) -> str:
        return self._relay.lan_link()

    def _on_copy_lan_link(self) -> None:
        url = self._lan_share_url()
        if url:
            QApplication.clipboard().setText(url)
            self._show_copied(self._copy_lan_link_btn, self._copy_lan_link_feedback)

    def _primary_ip(self) -> str:
        """LAN 配布に使うホストの IPv4 候補（推定）。失敗しても空文字で返し、
        meeting_start を落とさない。VPN/loopback/link-local は後回し。"""
        import socket
        candidates: list[str] = []
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if ip and ip not in candidates:
                    candidates.append(ip)
        except Exception:
            candidates = []

        def _rank(ip: str) -> tuple:
            # loopback を最後、link-local を次、VPN 帯(25./100.64.)を後ろ、私設 LAN を前。
            return (ip.startswith("127."), ip.startswith("169.254."),
                    ip.startswith("25.") or ip.startswith("100.64."), ip)

        for ip in sorted(candidates, key=_rank):
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
        return ""

    def _on_name_changed(self, text: str) -> None:
        self._relay.set_host_name(text)

    # ---- relay signals ----

    def _on_state(self, state: str) -> None:
        if state == "expired":
            self._token = ""
            self._token_edit.setText("")
            self._conn_label.setText(tr("meeting.state.expired"))
        elif state == "disconnected":
            self._conn_label.setText(tr("meeting.conn.disconnected"))
        elif state == "ok":
            self._conn_label.setText(tr("meeting.conn.ok"))
        elif state == "starting":
            self._log_line(tr("meeting.state.starting"))
        elif state == "external_unavailable":
            # 外部トークン表示のみクリア。LAN は relay.lan_link() live 照会で維持。
            self._token = ""
            self._token_edit.setText("")
            self._log_line(tr("meeting.state.external_unavailable"))
        elif state == "tunnel_failed":
            # 外部トークンのみクリア（LAN ボタンを道連れにしない）。
            self._token = ""
            self._token_edit.setText("")
            self._log_line(tr("meeting.state.tunnel_failed"))
        self._update_state_label()
        self._update_enabled()

    def _on_participants(self, parts) -> None:
        now = int(time.time())
        rows = []
        for p in (parts or []):
            age = max(0, now - int(p.get("last", now)))
            rows.append(tr("meeting.participant_row",
                           name=p.get("name", "") or tr("meeting.guest_default"),
                           age=age, tab=p.get("tab", "") or "-"))
        self._part_label.setText("\n".join(rows))

    def _on_remote_message(self, sid: str, name: str, text: str) -> None:
        self._log_line(f"{name}: {text}")

    # ---- periodic ----

    def _tick(self) -> None:
        self._update_remaining()
        self._refresh_lists()

    def _update_remaining(self) -> None:
        if not self._relay.is_sharing():
            self._remaining_label.setText("")
            return
        remaining = self._relay.expires_at() - int(time.time())
        if remaining < 0:
            remaining = 0
        h, rem = divmod(remaining, 3600)
        m, _s = divmod(rem, 60)
        self._remaining_label.setText(
            tr("meeting.remaining", remaining=f"{h}h{m:02d}m", period="")
        )

    # ---- list rendering ----

    def _current_published_sessions(self) -> set[str]:
        return self._relay.published_session_ids()

    def _refresh_lists(self) -> None:
        cw = self._window.chat_widget()
        summaries = cw.session_summaries() if cw is not None else []
        cur_ds = getattr(self._window, "current_dataset", None)
        sharing = self._relay.is_sharing()
        self._relay.absorb_new_sessions(summaries)
        published = self._relay.published_session_ids()
        # absorb は archived 込みの full summaries で known をマークできるが、以降の表示
        # ・件数（行・has_new）は archived を除外する。
        summaries = [s for s in summaries if not s.get("archived")]

        sig = tuple((s["id"], s["dataset"], s["title"]) for s in summaries) + (sharing,)
        if sig != self._sess_sig:
            self._sess_sig = sig
            self._rebuild_session_rows(summaries, cur_ds, sharing, published)

        # new-session note: any session created after start, not yet published.
        if sharing and not self._relay.auto_share_new_sessions():
            start_ids = self._relay.meeting_start_ids()
            has_new = any(
                s["id"] not in start_ids and s["id"] not in published
                for s in summaries
            )
            self._new_session_note.setVisible(has_new)
        else:
            self._new_session_note.setVisible(False)

        # Mid-meeting tabs auto-join the published set before we read it, so a
        # newly-opened tab renders checked immediately (no one-tick flicker). All
        # open DSs are shareable now (Issue #78), so enumerate every DS's tabs as
        # (ds-key, name) pairs (ds None → "").
        self._relay.absorb_new_tabs()
        tab_pairs = []
        for t in self._window.tabs():
            name = getattr(t, "name", None)
            if name is None:
                continue
            tds = (getattr(t, "session_spec", None) or {}).get("dataset")
            tab_pairs.append(("" if tds is None else tds, name))
        pub_tabs = self._relay.published_tabs()
        tsig = tuple(tab_pairs) + (sharing,)
        if tsig != self._tab_sig:
            self._tab_sig = tsig
            self._rebuild_tab_rows(tab_pairs, sharing, pub_tabs)

    def _rebuild_session_rows(self, summaries, cur_ds, sharing, published) -> None:
        while self._sess_layout.count():
            item = self._sess_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._sess_boxes = {}
        # group by dataset (None last)
        by_ds: dict = {}
        for s in summaries:
            by_ds.setdefault(s["dataset"], []).append(s)
        for ds in sorted(by_ds, key=lambda d: (d is None, d or "")):
            header = QLabel(str(ds) if ds is not None else "—")
            header.setStyleSheet("font-weight:bold;color:#6ec1e4")
            self._sess_layout.addWidget(header)
            for s in by_ds[ds]:
                box = QCheckBox(s["title"] or s["id"])
                if sharing:
                    box.setChecked(s["id"] in published)
                else:
                    box.setChecked(True)   # default scope = all sessions
                if ds != cur_ds:
                    box.setText(box.text() + " " + tr("meeting.other_dataset_note"))
                box.toggled.connect(self._on_session_toggle)
                self._sess_layout.addWidget(box)
                self._sess_boxes[s["id"]] = box
        self._sess_layout.addStretch(1)
        self._sync_select_all(self._sess_boxes, self._sess_select_all)

    def _rebuild_tab_rows(self, pairs, sharing, pub_tabs) -> None:
        while self._tab_layout.count():
            item = self._tab_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._tab_boxes = {}
        # group by dataset ("" = null group, sorted last) — mirrors _rebuild_session_rows
        by_ds: dict = {}
        for (ds, name) in pairs:
            by_ds.setdefault(ds, []).append(name)
        for ds in sorted(by_ds, key=lambda d: (d == "", d)):
            header = QLabel(ds if ds else "—")
            header.setStyleSheet("font-weight:bold;color:#6ec1e4")
            self._tab_layout.addWidget(header)
            for name in by_ds[ds]:
                box = QCheckBox(name)
                box.setChecked((ds, name) in pub_tabs if sharing else True)
                box.toggled.connect(self._on_tab_toggle)
                self._tab_layout.addWidget(box)
                self._tab_boxes[(ds, name)] = box
        self._tab_layout.addStretch(1)
        self._sync_select_all(self._tab_boxes, self._tab_select_all)

    def _on_session_toggle(self, _checked=False) -> None:
        if self._relay.is_sharing():
            checked = {sid for sid, box in self._sess_boxes.items() if box.isChecked()}
            visible = set(self._sess_boxes)                          # いま描画中の box の全 sid
            preserved = self._relay.published_session_ids() - visible  # 非表示だが published（archived 等）
            self._relay.set_published_sessions(checked | preserved)
        self._sync_select_all(self._sess_boxes, self._sess_select_all)

    def _on_auto_share_toggled(self, checked: bool) -> None:
        self._relay.set_auto_share_new_sessions(checked)
        self._sess_sig = None      # 行を再構築して、公開状態の変化を反映する
        self._refresh_lists()

    def _on_tab_toggle(self, _checked=False) -> None:
        if self._relay.is_sharing():
            keys = [key for key, box in self._tab_boxes.items() if box.isChecked()]
            self._relay.set_published_tabs(keys)
        self._sync_select_all(self._tab_boxes, self._tab_select_all)

    def _on_select_all_sessions(self, _checked=False) -> None:
        boxes = self._sess_boxes
        if not boxes:
            return
        target = not all(b.isChecked() for b in boxes.values())
        for b in boxes.values():
            b.blockSignals(True)
            b.setChecked(target)
            b.blockSignals(False)
        if self._relay.is_sharing():
            checked = {sid for sid, b in boxes.items() if b.isChecked()}
            preserved = self._relay.published_session_ids() - set(boxes)  # 非表示だが published を温存
            self._relay.set_published_sessions(checked | preserved)
        self._sync_select_all(boxes, self._sess_select_all)

    def _on_select_all_tabs(self, _checked=False) -> None:
        boxes = self._tab_boxes
        if not boxes:
            return
        target = not all(b.isChecked() for b in boxes.values())
        for b in boxes.values():
            b.blockSignals(True)
            b.setChecked(target)
            b.blockSignals(False)
        if self._relay.is_sharing():
            keys = [key for key, b in boxes.items() if b.isChecked()]
            self._relay.set_published_tabs(keys)
        self._sync_select_all(boxes, self._tab_select_all)

    def _sync_select_all(self, boxes, master) -> None:
        t = len(boxes)
        master.setEnabled(t > 0)
        n = sum(1 for b in boxes.values() if b.isChecked())
        if t == 0 or n == 0:
            master.setCheckState(Qt.CheckState.Unchecked)
        elif n == t:
            master.setCheckState(Qt.CheckState.Checked)
        else:
            master.setCheckState(Qt.CheckState.PartiallyChecked)

    # ---- helpers ----

    def _log_line(self, text: str) -> None:
        self._log.appendPlainText(time.strftime("%H:%M:%S ") + text)

    def _update_state_label(self) -> None:
        # relay.share_status() を SSOT にする（_token/_lan_link の有無から派生させると
        # external_unavailable が "sharing" に上書きされる。reviewer P1-1）。
        st = self._relay.share_status()
        label_key = {
            "sharing": "meeting.state.sharing",
            "external_unavailable": "meeting.state.external_unavailable",
            "starting": "meeting.state.starting",
            "tunnel_failed": "meeting.state.tunnel_failed",
            "idle": "meeting.state.idle",
        }.get(st, "meeting.state.idle")
        self._state_label.setText(tr(label_key))
        if self._relay.is_sharing():
            self._channel_label.setText(
                tr("meeting.label.channel") + ": " + (self._relay.channel() or ""))
        else:
            self._channel_label.setText("")
        self._relay_label.setText(tr("meeting.label.relay") + ": " + self._relay.base_url())

    def _update_enabled(self) -> None:
        configured = self._relay.is_configured()
        sharing = self._relay.is_sharing()
        lan_ready = bool(self._relay.lan_link())
        self._not_configured.setVisible(not configured)
        self._start_btn.setEnabled(configured and not sharing)
        self._ttl_combo.setEnabled(not sharing)
        self._stop_btn.setEnabled(sharing)
        self._lan_link_check.setEnabled(not sharing)     # 開始後は変更不可
        self._lan_host_edit.setEnabled(not sharing)
        self._copy_lan_link_btn.setEnabled(lan_ready)    # LAN リンク実在時のみ
        self._update_state_label()

    def retranslate(self) -> None:
        self.setWindowTitle(tr("meeting.share.title"))
        self._stop_btn.setText(tr("meeting.btn.stop"))
        self._ttl_caption.setText(tr("meeting.label.ttl"))
        self._name_caption.setText(tr("meeting.label.host_name"))
        self._start_btn.setText(tr("meeting.btn.start"))
        self._token_caption.setText(tr("meeting.label.token"))
        if self._copy_feedback.isActive():
            self._copy_btn.setText(tr("meeting.btn.copied"))
        else:
            self._copy_btn.setText(tr("meeting.btn.copy"))
        if self._copy_link_feedback.isActive():
            self._copy_link_btn.setText(tr("meeting.btn.copied"))
        else:
            self._copy_link_btn.setText(tr("meeting.btn.copy_link"))
        self._reveal_btn.setText(
            tr("meeting.btn.hide") if self._token_revealed else tr("meeting.btn.reveal")
        )
        if self._copy_lan_link_feedback.isActive():
            self._copy_lan_link_btn.setText(tr("meeting.btn.copied"))
        else:
            self._copy_lan_link_btn.setText(tr("meeting.btn.copy_lan_link"))
        self._lan_link_check.setText(tr("meeting.check.lan_link"))
        self._lan_host_caption.setText(tr("meeting.label.lan_host"))
        self._lan_https_hint.setText(tr("meeting.lan.https_hint"))
        self._token_hint.setText(tr("meeting.token.hint"))
        self._expiry_note.setText(tr("meeting.expiry.delayed"))
        self._sess_group.setTitle(tr("meeting.section.sessions"))
        self._sess_select_all.setText(tr("meeting.btn.select_all"))
        self._auto_share_new.setText(tr("meeting.btn.auto_share_new_sessions"))
        self._new_session_note.setText(tr("meeting.new_session_note"))
        self._tab_group.setTitle(tr("meeting.section.tabs"))
        self._tab_select_all.setText(tr("meeting.btn.select_all"))
        self._part_group.setTitle(tr("meeting.section.participants"))
        self._log_group.setTitle(tr("meeting.section.log"))
        self._not_configured.setText(tr("meeting.not_configured"))
        self._sess_sig = None    # force relabel of rows (other_dataset_note etc.)
        self._tab_sig = None
        self._update_state_label()
