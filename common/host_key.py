"""この PC の登録簿のキー（Issue #122）。Qt 非依存・config 非依存。

登録簿 ``datasets.local.json`` は ``{dataset: {HOST: path}}`` で、HOST はこの PC の名前を大文字にしたもの。

- Windows・Linux: ``socket.gethostname().upper()``（毎回呼び出し時に引く。キャッシュしない）。
- macOS: ``gethostname()`` は DHCP・ネットワークで変わるので、正規のキーを HostName（設定されていれば）→
  LocalHostName → ``gethostname()`` から ``.local`` を外したもの、の順で決める（scutil で読み、プロセスの中で 1 回だけ）。
  ``host_keys()`` は正規のキーの後に照合用の別名（旧コードのキー ``gethostname().upper()`` を必ず含む）を並べる。

書込は ``host_key()``、照合は ``host_keys()`` の順。
"""

from __future__ import annotations

import functools
import re
import socket
import subprocess
import sys

# ホスト名は照合前に .upper() 済み。先頭は英数字 (非ASCII の文字も可)、以降は
# 英数字・'_'・'.'・'-'。パス区切り・空白・記号・制御文字は拒否する。
# \w は Unicode 既定で非ASCII 文字にマッチするため、ドイツ語等の PC 名も許可。
HOST_RE = re.compile(r"^[^\W_][\w.\-]*$")
_SCUTIL = "/usr/sbin/scutil"
_SCUTIL_TIMEOUT = 2.0


def _is_darwin() -> bool:
    return sys.platform == "darwin"


def _scutil(key: str) -> str | None:
    """``scutil --get <key>`` の値。未設定（rc 1）・失敗・時間切れは None。投げない。"""
    try:
        r = subprocess.run([_SCUTIL, "--get", key], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=_SCUTIL_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    value = (r.stdout or "").strip()
    return value or None


@functools.lru_cache(maxsize=1)
def _darwin_names() -> tuple[str | None, str | None]:
    """(HostName, LocalHostName)。プロセスの中で 1 回だけ読む（変えたら再起動で反映）。"""
    return _scutil("HostName"), _scutil("LocalHostName")


def cache_clear() -> None:
    _darwin_names.cache_clear()


def _valid(name: str | None) -> str | None:
    if not name:
        return None
    upper = name.upper()
    return upper if HOST_RE.fullmatch(upper) else None


def _strip_local(name: str) -> str:
    return name[:-len(".local")] if name.lower().endswith(".local") else name


def host_key() -> str:
    """この PC の登録簿の正規のキー（書込に使う）。"""
    if not _is_darwin():
        return socket.gethostname().upper()
    host, local = _darwin_names()
    stripped = _strip_local(socket.gethostname())
    for name in (host, local, stripped):
        key = _valid(name)
        if key is not None:
            return key
    return socket.gethostname().upper()


def host_keys() -> list[str]:
    """照合に使うキー。先頭は ``host_key()``、続いて別名（macOS だけ）。"""
    if not _is_darwin():
        return [socket.gethostname().upper()]
    out = [host_key()]
    host, local = _darwin_names()
    candidates = [host, local]
    if local:
        candidates.append(local + ".LOCAL")
    candidates.append(socket.gethostname())
    for name in candidates:
        key = _valid(name)
        if key is not None and key not in out:
            out.append(key)
    return out
