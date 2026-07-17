"""Public tunnel management (Issue #44) — pluggable provider behind one API.

Exposes the local relay (``127.0.0.1:<port>``) to external guests over a public
HTTPS URL. The provider is selectable via ``RELAY_TUNNEL`` so we can swap
transports without touching ``meeting/relay.py`` (which only ever calls
``Tunnel().start(port) -> url`` / ``stop()``):

  - ``cloudflared`` (default): Cloudflare **named** tunnel → a FIXED custom
                  hostname (``CLOUDFLARE_TUNNEL_HOSTNAME``, e.g.
                  ``relay.<your-domain>``). $0, stable URL, broad reachability.
                  Needs a one-time setup (``cloudflared tunnel login`` +
                  ``create`` + ``route dns``); see relay-worker/README.md.
  - ``pinggy``    : SSH reverse tunnel over port 443 → ``*.pinggy.link``. No
                  account, HTTPS; free sessions expire ~60 min (reconnect ⇒ new URL).
  - ``tailscale`` : Tailscale Funnel → ``*.ts.net``. $0/no-caps but its public
                  ingress proved flaky for arbitrary guest networks here.

A named tunnel rides your own Cloudflare-delegated domain, so it sidesteps the
quick-tunnel pitfall where some networks DNS-block ``trycloudflare.com``
(``api.trycloudflare.com`` → gaierror).

``Tunnel`` is a thin FACADE that picks a provider and delegates; patching
``Tunnel.start`` in tests still works regardless of the selected provider.

THREAD CONTRACT: ``start()`` runs on ``_TunnelStarter`` (worker thread); ``stop()``
runs on the GUI thread. Providers serialize via a lock + ``_stop_requested`` so a
stop racing ahead of start can't orphan a process / leave a tunnel up.
"""

from __future__ import annotations

import json
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
    """Selects a provider by ``RELAY_TUNNEL`` (default ``pinggy``) and delegates."""

    def __init__(self, provider: "str | None" = None) -> None:
        name = (provider or os.environ.get("RELAY_TUNNEL") or "cloudflared").strip().lower()
        self._impl = _make_provider(name)

    def start(self, port: int, **kw) -> str:
        return self._impl.start(port, **kw)

    def stop(self) -> None:
        self._impl.stop()


def _make_provider(name: str):
    if name in ("cloudflared", "cloudflare", "cf", "named"):
        return _CloudflaredNamed()
    if name == "pinggy":
        return _PinggyTunnel()
    if name in ("tailscale", "funnel"):
        return _TailscaleFunnel()
    raise RuntimeError(
        f"unknown RELAY_TUNNEL={name!r} (expected: cloudflared | pinggy | tailscale)")


# --------------------------------------------------------------------------- #
# cloudflared — Cloudflare named tunnel (default)
# --------------------------------------------------------------------------- #
class _CloudflaredNamed:
    """`cloudflared tunnel run --url http://127.0.0.1:<port> <name>` → a FIXED
    ``https://<CLOUDFLARE_TUNNEL_HOSTNAME>``.

    Unlike pinggy/quick-tunnel the public URL is known a priori (the hostname was
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


# --------------------------------------------------------------------------- #
# pinggy — SSH reverse tunnel (default)
# --------------------------------------------------------------------------- #
class _PinggyTunnel:
    """`ssh -p 443 -R0:127.0.0.1:<port> a.pinggy.io http` → prints an HTTPS URL.

    SSH over 443 (not 22) so it traverses outbound-443-only networks. Anonymous
    (no key/password); BatchMode + stdin=DEVNULL keep it non-interactive.
    """

    # pinggy free emits several URLs (e.g. https://<id>.run.pinggy-free.link and
    # https://<id>.free.pinggy.net); match the first HTTPS one that is a pinggy host.
    _URL_RE = re.compile(r"https://[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

    def __init__(self) -> None:
        self._ssh = os.environ.get("SSH_BIN") or "ssh"
        self._lock = threading.Lock()
        self._proc: "subprocess.Popen | None" = None
        self._stop_requested = False

    def start(self, port: int, *, timeout: float = 25.0) -> str:
        with self._lock:
            if self._stop_requested:
                raise RuntimeError("tunnel start aborted: stop already requested")
            argv = [
                self._ssh, "-p", "443",
                "-o", "StrictHostKeyChecking=no",
                "-o", "BatchMode=yes",
                "-o", "ServerAliveInterval=30",
                "-o", "ExitOnForwardFailure=yes",
                "-R", f"0:127.0.0.1:{port}", "a.pinggy.io",
            ]
            try:
                self._proc = subprocess.Popen(
                    argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, text=True,
                    # decode as UTF-8 with replacement so the reader can't die on the
                    # platform default codec (cp932 on Windows) if ssh prints non-ASCII.
                    encoding="utf-8", errors="replace",
                    **no_window_kwargs(),
                )
            except FileNotFoundError as e:
                raise RuntimeError(
                    f"ssh not found (install the OpenSSH client or set SSH_BIN): {e!r}"
                ) from e
            proc = self._proc

        # Bounded + drop-on-Full so the long-lived reader can't grow memory once the
        # URL is found and nobody consumes the queue (see _CloudflaredNamed).
        q: "queue.Queue[str | None]" = queue.Queue(maxsize=2000)

        def _reader() -> None:
            try:
                for line in proc.stdout:
                    try:
                        q.put_nowait(line)
                    except queue.Full:
                        pass
            except Exception:
                pass
            finally:
                try:
                    q.put_nowait(None)   # sentinel: EOF / process exit
                except queue.Full:
                    pass

        threading.Thread(target=_reader, daemon=True).start()

        # pinggy prints several tunnel URLs in one burst (e.g. *.run.pinggy-free.link
        # AND *.free.pinggy.net). The *.pinggy-free.link host has proven unreachable
        # from some external networks (ECONNREFUSED), while *.pinggy.net works — so
        # collect for a short settle window and PREFER a *.pinggy.net URL.
        deadline = time.monotonic() + timeout
        settle_until: "float | None" = None
        collected: "list[str]" = []
        while True:
            now = time.monotonic()
            if settle_until is not None and now >= settle_until:
                break
            if not collected and now >= deadline:
                self.stop()
                raise TimeoutError(f"pinggy did not emit a URL within {timeout}s")
            wait = max(0.05, (settle_until if settle_until is not None else deadline) - now)
            try:
                line = q.get(timeout=wait)
            except queue.Empty:
                if collected:
                    break
                self.stop()
                raise TimeoutError(f"pinggy did not emit a URL within {timeout}s")
            if line is None:
                if collected:
                    break
                raise RuntimeError("pinggy (ssh) exited before emitting a tunnel URL")
            for u in self._URL_RE.findall(line):
                # tunnel hosts only — skip the dashboard.pinggy.io marketing link.
                if "pinggy" in u and "pinggy.io" not in u:
                    u = u.rstrip("/")
                    if u not in collected:
                        collected.append(u)
                        if settle_until is None:
                            settle_until = time.monotonic() + 2.0  # grace for a better domain
        for u in collected:
            if ".pinggy.net" in u:
                return u
        return collected[0]

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


# --------------------------------------------------------------------------- #
# Tailscale Funnel (kept as a selectable fallback)
# --------------------------------------------------------------------------- #
def _default_ts_bin() -> str:
    env = os.environ.get("TAILSCALE_BIN")
    if env:
        return env
    if os.name == "nt":
        return r"C:\Program Files\Tailscale\tailscale.exe"
    return "tailscale"


class _TailscaleFunnel:
    """`tailscale funnel --bg <port>` → `https://<host>.<tailnet>.ts.net`.

    Prereqs (out of code): Tailscale signed in, HTTPS certs enabled, Funnel
    node-attr granted. start() surfaces Tailscale's own stderr on failure.
    """

    def __init__(self, bin_path: "str | None" = None) -> None:
        self._bin = bin_path or _default_ts_bin()
        self._lock = threading.Lock()
        self._stop_requested = False
        self._on = False

    def _run(self, args: "list[str]", timeout: float) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            [self._bin, *args], capture_output=True, text=True, timeout=timeout,
            **no_window_kwargs(),
        )

    def start(self, port: int, *, timeout: float = 20.0) -> str:
        with self._lock:
            if self._stop_requested:
                raise RuntimeError("tunnel start aborted: stop already requested")
            try:
                r = self._run(["status", "--json"], timeout)
            except FileNotFoundError as e:
                raise RuntimeError(
                    f"tailscale not found (install Tailscale or set TAILSCALE_BIN): {e!r}"
                ) from e
            except subprocess.TimeoutExpired as e:
                raise RuntimeError("tailscale status timed out") from e
            if r.returncode != 0:
                raise RuntimeError(f"tailscale status failed: {(r.stderr or r.stdout).strip()}")
            try:
                st = json.loads(r.stdout)
            except Exception as e:
                raise RuntimeError(f"could not parse tailscale status: {e!r}") from e
            state = st.get("BackendState")
            if state != "Running":
                raise RuntimeError(
                    f"Tailscale is not running (BackendState={state!r}). Run `tailscale up` and sign in.")
            dns = (st.get("Self") or {}).get("DNSName", "").rstrip(".")
            if not dns:
                raise RuntimeError(
                    "Tailscale Self.DNSName is empty — enable MagicDNS + HTTPS certificates.")
            try:
                f = self._run(["funnel", "--bg", str(port)], timeout)
            except subprocess.TimeoutExpired as e:
                raise RuntimeError("tailscale funnel timed out") from e
            if f.returncode != 0:
                raise RuntimeError(f"tailscale funnel failed: {(f.stderr or f.stdout).strip()}")
            self._on = True
            return f"https://{dns}"

    def stop(self) -> None:
        with self._lock:
            self._stop_requested = True
            was_on = self._on
            self._on = False
        if was_on:
            try:
                self._run(["funnel", "reset"], 10.0)
            except Exception:
                pass
