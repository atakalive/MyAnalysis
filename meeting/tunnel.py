"""Public tunnel management (Issue #44) — Cloudflare named tunnel.

Exposes the local relay (``127.0.0.1:<port>``) to external guests over a public
HTTPS URL via a Cloudflare **named** tunnel → a FIXED custom hostname
(``CLOUDFLARE_TUNNEL_HOSTNAME``, e.g. ``relay.<your-domain>``). $0, stable URL,
broad reachability. Needs a one-time setup (``cloudflared tunnel login`` +
``create`` + ``route dns``); see relay-worker/README.md.

A named tunnel rides your own Cloudflare-delegated domain, so it sidesteps the
quick-tunnel pitfall where some networks DNS-block ``trycloudflare.com``
(``api.trycloudflare.com`` → gaierror).

``Tunnel`` is a thin FACADE over ``_CloudflaredNamed``; it only ever exposes
``Tunnel().start(port) -> url`` / ``stop()`` to ``meeting/relay.py``, and patching
``Tunnel.start`` in tests keeps working.

THREAD CONTRACT: ``start()`` runs on ``_TunnelStarter`` (worker thread); ``stop()``
runs on the GUI thread. It serializes via a lock + ``_stop_requested`` so a
stop racing ahead of start can't orphan a process / leave a tunnel up.
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
import time

from common.proc import no_window_kwargs


# --------------------------------------------------------------------------- #
# Facade
# --------------------------------------------------------------------------- #
class Tunnel:
    """Exposes the local relay over a fixed public HTTPS URL via a Cloudflare
    named tunnel. Thin facade over ``_CloudflaredNamed`` — kept so tests can patch
    ``Tunnel.start`` and the start(worker-thread)/stop(GUI-thread) contract stays
    documented in one place."""

    def __init__(self) -> None:
        self._impl = _CloudflaredNamed()

    def start(self, port: int, **kw) -> str:
        return self._impl.start(port, **kw)

    def stop(self) -> None:
        self._impl.stop()


# --------------------------------------------------------------------------- #
# cloudflared — Cloudflare named tunnel (default)
# --------------------------------------------------------------------------- #
class _CloudflaredNamed:
    """`cloudflared tunnel run --url http://127.0.0.1:<port> <name>` → a FIXED
    ``https://<CLOUDFLARE_TUNNEL_HOSTNAME>``.

    Unlike a quick tunnel, the public URL is known a priori (the hostname was
    pre-routed via ``cloudflared tunnel route dns``), so we don't scrape it from
    output — we wait until cloudflared registers an edge connection, then return
    the configured hostname. ``--url`` overrides ingress so the relay's *ephemeral*
    port need not be baked into a config file.

    One-time setup (manual; see relay-worker/README.md): domain delegated to
    Cloudflare, then ``cloudflared tunnel login`` + ``create`` + ``route dns``.
    Env: ``CLOUDFLARE_TUNNEL_NAME`` (name or UUID), ``CLOUDFLARE_TUNNEL_HOSTNAME``
    (the routed hostname), ``CLOUDFLARED_BIN`` (optional bin path),
    ``CLOUDFLARE_TUNNEL_CRED`` (optional credentials-file; lets a second PC run by
    UUID without ``cloudflared tunnel login``).
    """

    # cloudflared logs one line per edge connection once it's live (current build:
    # "Registered tunnel connection ..."; older builds: "Connection <id> registered").
    _READY_RE = re.compile(r"Registered tunnel connection|Connection .* registered", re.I)

    def __init__(self) -> None:
        self._bin = os.environ.get("CLOUDFLARED_BIN") or "cloudflared"
        self._name = (os.environ.get("CLOUDFLARE_TUNNEL_NAME") or "").strip()
        self._host = (os.environ.get("CLOUDFLARE_TUNNEL_HOSTNAME") or "").strip().rstrip("/")
        self._cred = os.environ.get("CLOUDFLARE_TUNNEL_CRED")
        self._lock = threading.Lock()
        self._proc: "subprocess.Popen | None" = None
        self._stop_requested = False

    def start(self, port: int, *, timeout: float = 30.0) -> str:
        with self._lock:
            if self._stop_requested:
                raise RuntimeError("tunnel start aborted: stop already requested")
            if not self._name:
                raise RuntimeError(
                    "CLOUDFLARE_TUNNEL_NAME not set (cloudflared tunnel name or UUID)")
            if not self._host:
                raise RuntimeError(
                    "CLOUDFLARE_TUNNEL_HOSTNAME not set (e.g. relay.example.com)")
            argv = [
                self._bin, "--no-autoupdate", "--loglevel", "info",
                "tunnel", "run", "--url", f"http://127.0.0.1:{port}",
                *(["--credentials-file", self._cred] if self._cred else []),
                self._name,
            ]
            try:
                self._proc = subprocess.Popen(
                    argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, text=True,
                    # cloudflared emits UTF-8 (incl. the localized network-adapter
                    # name, e.g. Japanese); decode as UTF-8 with replacement so the
                    # reader can't die on the platform default codec (cp932 on Windows).
                    encoding="utf-8", errors="replace",
                    **no_window_kwargs(),
                )
            except FileNotFoundError as e:
                raise RuntimeError(
                    "cloudflared not found (winget install cloudflare.cloudflared "
                    f"or set CLOUDFLARED_BIN): {e!r}"
                ) from e
            proc = self._proc

        # Bounded: the reader must keep draining stdout for the tunnel's whole life
        # (else cloudflared's pipe fills and stalls it), but after start() returns
        # nobody consumes the queue — so cap it and drop on Full to avoid unbounded
        # memory growth across a (up to 24h) meeting.
        q: "queue.Queue[str | None]" = queue.Queue(maxsize=2000)

        def _reader() -> None:
            try:
                for line in proc.stdout:
                    try:
                        q.put_nowait(line)
                    except queue.Full:
                        pass   # post-readiness: no consumer; discard
            except Exception:
                pass
            finally:
                try:
                    q.put_nowait(None)   # sentinel: EOF / process exit
                except queue.Full:
                    pass

        threading.Thread(target=_reader, daemon=True).start()

        # The named tunnel's hostname is fixed/known; just wait for cloudflared to
        # register an edge connection, then return it. stop() terminates the proc,
        # which closes stdout → reader emits the sentinel → this loop unblocks.
        deadline = time.monotonic() + timeout
        tail: "list[str]" = []
        while True:
            now = time.monotonic()
            if now >= deadline:
                self.stop()
                raise TimeoutError(f"cloudflared did not register within {timeout}s")
            try:
                line = q.get(timeout=max(0.05, deadline - now))
            except queue.Empty:
                continue
            if line is None:
                raise RuntimeError(
                    "cloudflared exited before registering: "
                    + (" | ".join(tail[-5:]).strip() or "<no output>"))
            tail.append(line.strip())
            if self._READY_RE.search(line):
                return f"https://{self._host}"

    def stop(self) -> None:
        with self._lock:
            self._stop_requested = True
            proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:
                pass
