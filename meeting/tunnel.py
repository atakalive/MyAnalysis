"""Tailscale Funnel management (Issue #44).

Exposes the local relay (``127.0.0.1:<port>``) to external guests over a public
HTTPS ``https://<host>.<tailnet>.ts.net`` URL using **Tailscale Funnel**.

Why not cloudflared quick tunnel: some networks DNS-block
``trycloudflare.com`` specifically (``api.trycloudflare.com`` -> gaierror), while
Tailscale's infra resolves fine, and the host has no inbound port-opening
(outbound-only). Funnel is outbound-only, free, gives a stable real-HTTPS URL
(guests get a secure context), and guests need no Tailscale of their own.

The ``Tunnel`` public API (``start(port) -> url`` / ``stop()``) is unchanged from
the cloudflared version, so ``meeting/relay.py`` is untouched.

THREAD CONTRACT: ``start()`` runs on ``_TunnelStarter`` (a worker thread);
``stop()`` runs on the GUI thread. They serialize through ``self._lock`` +
``self._stop_requested`` so a stop that races ahead of start can't leave Funnel
enabled. The Tailscale CLI calls are quick (they mutate serve config and return),
so holding the lock across ``start()`` does not stall the GUI like the old
cloudflared URL-wait would have.

PREREQUISITES (one-time, OUT of code — surfaced as errors if missing):
  1. Tailscale installed + signed in (``tailscale up``).
  2. HTTPS certificates enabled for the tailnet (admin console -> DNS).
  3. Funnel node-attribute granted in the tailnet policy (admin console ->
     Access controls -> nodeAttrs ``funnel``). The first ``tailscale funnel`` run
     prints an enable URL if this is missing; that text is surfaced verbatim.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading


def _default_bin() -> str:
    env = os.environ.get("TAILSCALE_BIN")
    if env:
        return env
    if os.name == "nt":
        return r"C:\Program Files\Tailscale\tailscale.exe"
    return "tailscale"


class Tunnel:
    def __init__(self, bin_path: "str | None" = None) -> None:
        self._bin = bin_path or _default_bin()
        self._lock = threading.Lock()
        self._stop_requested = False
        self._on = False

    def _run(self, args: "list[str]", timeout: float) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            [self._bin, *args],
            capture_output=True, text=True, timeout=timeout,
        )

    def start(self, port: int, *, timeout: float = 20.0) -> str:
        with self._lock:
            if self._stop_requested:
                raise RuntimeError("tunnel start aborted: stop already requested")

            # 1) Backend must be Running; derive the public host from Self.DNSName.
            try:
                r = self._run(["status", "--json"], timeout)
            except FileNotFoundError as e:
                raise RuntimeError(
                    f"tailscale not found (install Tailscale or set TAILSCALE_BIN): {e!r}"
                ) from e
            except subprocess.TimeoutExpired as e:
                raise RuntimeError("tailscale status timed out") from e
            if r.returncode != 0:
                raise RuntimeError(
                    f"tailscale status failed: {(r.stderr or r.stdout).strip()}")
            try:
                st = json.loads(r.stdout)
            except Exception as e:
                raise RuntimeError(f"could not parse tailscale status: {e!r}") from e

            state = st.get("BackendState")
            if state != "Running":
                raise RuntimeError(
                    f"Tailscale is not running (BackendState={state!r}). "
                    "Run `tailscale up` and sign in.")
            dns = (st.get("Self") or {}).get("DNSName", "").rstrip(".")
            if not dns:
                raise RuntimeError(
                    "Tailscale Self.DNSName is empty — enable MagicDNS + HTTPS "
                    "certificates in the admin console.")

            # 2) Point Funnel at the current ephemeral relay port (background).
            try:
                f = self._run(["funnel", "--bg", str(port)], timeout)
            except subprocess.TimeoutExpired as e:
                raise RuntimeError("tailscale funnel timed out") from e
            if f.returncode != 0:
                # When Funnel isn't permitted yet, the CLI prints an enable URL on
                # stderr; surface it verbatim so the user can click through.
                raise RuntimeError(
                    f"tailscale funnel failed: {(f.stderr or f.stdout).strip()}")

            self._on = True
            return f"https://{dns}"

    def stop(self) -> None:
        with self._lock:
            self._stop_requested = True
            was_on = self._on
            self._on = False
        if was_on:
            try:
                # `tailscale funnel reset` clears the funnel config (verified on
                # 1.98.4; there is no `funnel off` subcommand).
                self._run(["funnel", "reset"], 10.0)
            except Exception:
                pass   # best-effort; leaving a funnel up is harmless until next stop
