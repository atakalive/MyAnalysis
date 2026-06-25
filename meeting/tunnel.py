"""cloudflared quick-tunnel management (Issue #44).

Spawns ``cloudflared tunnel --url http://127.0.0.1:<port>`` and extracts the
``*.trycloudflare.com`` URL from its stderr. The local relay binds 127.0.0.1
only; the tunnel is the sole path that exposes it to guests.

THREAD CONTRACT: ``start()`` runs on ``_TunnelStarter`` (a worker thread);
``stop()`` runs on the GUI thread. Both serialize through ``self._lock`` +
``self._stop_requested`` so a stop that races ahead of start can't miss the
Popen and orphan a cloudflared process.
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
import time

_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class Tunnel:
    def __init__(self, bin_path: "str | None" = None) -> None:
        self._bin = bin_path or os.environ.get("CLOUDFLARED_BIN") or "cloudflared"
        self._lock = threading.Lock()
        self._proc: "subprocess.Popen | None" = None
        self._stop_requested = False

    def start(self, port: int, *, timeout: float = 15.0) -> str:
        with self._lock:
            if self._stop_requested:
                raise RuntimeError("tunnel start aborted: stop already requested")
            try:
                self._proc = subprocess.Popen(
                    [self._bin, "tunnel", "--url", f"http://127.0.0.1:{port}"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                )
            except FileNotFoundError as e:
                raise RuntimeError(
                    f"cloudflared not found (set CLOUDFLARED_BIN to override): {e!r}"
                ) from e
            proc = self._proc

        q: "queue.Queue[str | None]" = queue.Queue()

        def _reader() -> None:
            try:
                for line in proc.stdout:
                    q.put(line)
            except Exception:
                pass
            finally:
                q.put(None)   # sentinel: EOF / process exit

        threading.Thread(target=_reader, daemon=True).start()

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.stop()
                raise TimeoutError(
                    f"cloudflared did not emit a URL within {timeout}s")
            try:
                line = q.get(timeout=remaining)
            except queue.Empty:
                self.stop()
                raise TimeoutError(
                    f"cloudflared did not emit a URL within {timeout}s")
            if line is None:
                raise RuntimeError(
                    "cloudflared exited before emitting a tunnel URL")
            m = _URL_RE.search(line)
            if m:
                return m.group(0).rstrip("/")

    def stop(self) -> None:
        with self._lock:
            self._stop_requested = True
            proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:
                pass
