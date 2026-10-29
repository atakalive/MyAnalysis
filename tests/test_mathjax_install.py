"""common/mathjax_install.py — 数式描画モジュールの導入・削除・使用中の印（Issue #123）。

取得は偽物（``_urlopen`` の差し替え）で、ネットは使わない。導入先は conftest が tmp に向けている。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import tarfile
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from common import mathjax_install as mi
from common.filelock import exclusive_lock

V = mi.VERSION


def _tgz(entries: list[tuple[str, bytes | None, str]]) -> bytes:
    """entries: (名前, 中身, 種類 "file"/"dir"/"symlink"/"hardlink")。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data, kind in entries:
            ti = tarfile.TarInfo(name)
            if kind == "file":
                ti.size = len(data or b"")
                tar.addfile(ti, io.BytesIO(data or b""))
            elif kind == "dir":
                ti.type = tarfile.DIRTYPE
                tar.addfile(ti)
            elif kind == "symlink":
                ti.type = tarfile.SYMTYPE
                ti.linkname = "../../etc/passwd"
                tar.addfile(ti)
            elif kind == "hardlink":
                ti.type = tarfile.LNKTYPE
                ti.linkname = "package/LICENSE"
                tar.addfile(ti)
    return buf.getvalue()


def _mathjax_tgz(extra=()) -> bytes:
    return _tgz([
        ("package/tex-svg.js", b"// tex-svg", "file"),
        ("package/LICENSE", b"Apache-2.0", "file"),
        ("package/input/tex/extensions/ams.js", b"// ams", "file"),
        ("package/sre/speech.js", b"// sre", "file"),
        *extra,
    ])


def _font_tgz(font: str) -> bytes:
    return _tgz([
        ("package/svg.js", f"// {font}".encode(), "file"),
        ("package/svg/dynamic/a.js", b"// a", "file"),
        ("package/chtml.js", b"// chtml", "file"),
        ("package/package.json", b"{}", "file"),
    ])


def _integrity(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


class _Resp:
    def __init__(self, data: bytes, gate: threading.Event | None, reached: threading.Event | None):
        self._f = io.BytesIO(data)
        self._gate, self._reached = gate, reached
        self.headers = {"Content-Length": str(len(data))}

    def read(self, n):
        if self._reached is not None:
            self._reached.set()
        if self._gate is not None:
            assert self._gate.wait(10)
        return self._f.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def net(monkeypatch):
    """偽のレジストリ。blobs を書き換えると取得される中身が変わる（PINNED の integrity も合わせる）。"""
    blobs = {mi.MATHJAX_PACKAGE: _mathjax_tgz()}
    for f in mi.FONTS:
        blobs[mi.FONT_PACKAGES[f]] = _font_tgz(f)
    state = {"gate": None, "reached": None, "gate_pkg": None, "calls": []}

    def pin():
        pinned = {name: replace(pkg, integrity=_integrity(blobs[name]))
                  for name, pkg in mi.PINNED.items()}
        monkeypatch.setattr(mi, "PINNED", pinned)

    def urlopen(url, timeout):
        state["calls"].append(url)
        for name, pkg in mi.PINNED.items():
            if pkg.tarball == url:
                gated = state["gate_pkg"] in (None, name)
                return _Resp(blobs[name], state["gate"] if gated else None,
                             state["reached"] if gated else None)
        raise OSError(f"unknown url {url}")

    pin()
    monkeypatch.setattr(mi, "_urlopen", urlopen)
    state["blobs"], state["pin"] = blobs, pin
    return state


def _ok(font="mathjax-dejavu", cancelled=lambda: False):
    return mi.install(font, lambda d, t: None, cancelled)


def _root() -> Path:
    return mi.install_root()


def _workdirs():
    return sorted(p.name for p in _root().iterdir() if ".tmp-" in p.name) if _root().exists() else []


def _install_marks():
    d = _root() / "inuse"
    return sorted(p.name for p in d.glob("install-*.lock")) if d.exists() else []


def _hold(path: Path):
    """別の open で OS のロックを持つ（ほかの GUI の代わり）。戻り値の fd を閉じると外れる。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT)
    assert mi._try_lock(fd)
    return fd


# ---------------------------------------------------------------- 基本

def test_install_and_status(net):
    assert mi.status().state == "missing"
    r = _ok()
    assert r.ok, r
    st = mi.status()
    assert st.state == "installed" and st.fonts == ("mathjax-dejavu",)
    v = _root() / V
    assert (v / "tex-svg.js").read_text() == "// tex-svg"
    assert (v / "input" / "tex" / "extensions" / "ams.js").is_file()
    assert not (v / "sre").exists()
    assert (v / "fonts" / "mathjax-dejavu" / "svg.js").is_file()
    assert (v / "fonts" / "mathjax-dejavu" / "svg" / "dynamic" / "a.js").is_file()
    assert not (v / "fonts" / "mathjax-dejavu" / "chtml.js").exists()
    data = json.loads((v / "installed.json").read_text())
    assert data["version"] == V and data["fonts"] == ["mathjax-dejavu"]
    assert data["integrity"][mi.MATHJAX_PACKAGE] == mi.PINNED[mi.MATHJAX_PACKAGE].integrity
    assert _workdirs() == [] and _install_marks() == []
    assert mi.is_usable("mathjax-dejavu") and not mi.is_usable("mathjax-fira")


def test_pinned_constants_match_issue():
    assert mi.FONTS == ("mathjax-fira", "mathjax-dejavu", "mathjax-stix2", "mathjax-newcm")
    assert mi.DEFAULT_FONT == "mathjax-dejavu"
    assert mi.SCALES == (1.0, 1.1, 1.2, 1.35) and mi.DEFAULT_SCALE == 1.2
    assert mi.DEFAULT_BOLD is True and mi.MIN_SCALE == 0.7 and mi.BOLD_STROKE == 15
    for pkg in mi.PINNED.values():
        assert pkg.version == "4.1.3" and pkg.integrity.startswith("sha512-")
        assert pkg.tarball.startswith("https://registry.npmjs.org/")
    assert mi.font_package("mathjax-dejavu").npm_page == \
        "https://www.npmjs.com/package/@mathjax/mathjax-dejavu-font/v/4.1.3"
    assert mi.mathjax_package().npm_page == "https://www.npmjs.com/package/mathjax/v/4.1.3"


def test_integrity_mismatch_leaves_nothing(net, monkeypatch):
    net["blobs"][mi.MATHJAX_PACKAGE] = _mathjax_tgz() + b"x"    # PINNED は元のまま
    r = _ok()
    assert not r.ok and r.error == "integrity"
    assert not (_root() / V).exists()
    assert _workdirs() == [] and _install_marks() == []
    assert mi.status().state == "missing"


@pytest.mark.parametrize("bad", [
    [("outside/evil.js", b"x", "file")],
    [("package/../evil.js", b"x", "file")],
    [("/abs/evil.js", b"x", "file")],
    [("package/link.js", None, "symlink")],
    [("package/hard.js", None, "hardlink")],
])
def test_bad_archive_rejected(net, bad):
    net["blobs"][mi.MATHJAX_PACKAGE] = _mathjax_tgz(extra=bad)
    net["pin"]()
    r = _ok()
    assert not r.ok and r.error == "archive", r
    assert not (_root() / V).exists()
    assert _workdirs() == [] and _install_marks() == []


def test_cancel_restores_previous_state(net):
    assert _ok().ok
    before = sorted(str(p.relative_to(_root())) for p in _root().rglob("*"))
    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 2
    r = mi.install("mathjax-fira", lambda d, t: None, cancelled)
    assert not r.ok and r.error == "cancelled"
    after = sorted(str(p.relative_to(_root())) for p in _root().rglob("*"))
    assert after == before
    assert mi.status().fonts == ("mathjax-dejavu",)


def test_network_error(net, monkeypatch):
    def boom(url, timeout):
        raise OSError("connection refused")
    monkeypatch.setattr(mi, "_urlopen", boom)
    r = _ok()
    assert not r.ok and r.error == "network" and "refused" in r.message
    assert _workdirs() == [] and _install_marks() == []


def test_broken_and_outdated(net):
    assert _ok().ok
    (_root() / V / "installed.json").unlink()
    assert mi.status().state == "broken"
    assert _ok().ok                                   # 3-3: 壊れた版を置き換える
    assert mi.status().state == "installed"
    os.replace(_root() / V, _root() / "4.0.0")
    data = json.loads((_root() / "4.0.0" / "installed.json").read_text())
    data["version"] = "4.0.0"
    (_root() / "4.0.0" / "installed.json").write_text(json.dumps(data))
    st = mi.status()
    assert st.state == "outdated" and st.version == "4.0.0"
    assert _ok().ok
    assert mi.status().state == "installed"
    assert not (_root() / "4.0.0").exists()         # 手順 4 で古い版を消す


def test_already_complete_is_noop(net):
    assert _ok().ok
    info = _root() / V / "installed.json"
    mtime = info.stat().st_mtime_ns
    content = info.read_text()
    marker = _root() / V / "tex-svg.js"
    marker.write_text("// mine")
    assert _ok().ok
    assert info.read_text() == content and info.stat().st_mtime_ns == mtime
    assert marker.read_text() == "// mine"
    assert _workdirs() == [] and _install_marks() == []


def test_add_second_font(net):
    assert _ok("mathjax-dejavu").ok
    assert _ok("mathjax-fira").ok
    data = json.loads((_root() / V / "installed.json").read_text())
    assert data["fonts"] == ["mathjax-dejavu", "mathjax-fira"]
    assert (_root() / V / "fonts" / "mathjax-fira" / "svg.js").is_file()
    assert mi.status().fonts == ("mathjax-fira", "mathjax-dejavu")
    assert not mi.is_usable("mathjax-stix2")
    assert mi.acquire_use(V, "mathjax-stix2") is None


def test_partial_font_dir_is_replaced(net):
    assert _ok("mathjax-dejavu").ok
    junk = _root() / V / "fonts" / "mathjax-fira"
    junk.mkdir(parents=True)
    (junk / "half.js").write_text("x")
    assert _ok("mathjax-fira").ok
    assert not (junk / "half.js").exists()
    assert (junk / "svg.js").read_text() == "// mathjax-fira"


def test_missing_files_are_not_reported(net):
    assert _ok("mathjax-dejavu").ok
    assert _ok("mathjax-fira").ok
    (_root() / V / "fonts" / "mathjax-fira" / "svg.js").unlink()
    assert mi.status().fonts == ("mathjax-dejavu",)
    (_root() / V / "tex-svg.js").unlink()
    assert mi.status().state == "broken"


def test_uninstall_removes(net):
    assert _ok().ok
    r = mi.uninstall()
    assert r.ok
    assert not (_root() / V).exists()
    assert mi.status().state == "missing"
    assert (_root() / "install.lock").exists() and (_root() / "inuse").is_dir()
    assert (_root() / "generation").read_text().strip()


# ---------------------------------------------------------------- 使用中の印

def test_in_use_marks(net):
    assert _ok().ok
    os.replace(_root() / V, _root() / "4.0.0")       # 古い版に見立てる
    fd = _hold(_root() / "inuse" / "4.0.0-other.lock")
    try:
        assert mi.in_use_by_others("4.0.0")
        assert _ok().ok
        assert (_root() / "4.0.0").exists()           # 使用中の古い版は消さない
        r = mi.uninstall()
        assert not r.ok and r.error == "in_use"
        assert (_root() / "4.0.0").exists() and (_root() / V).exists()
    finally:
        os.close(fd)
    # 持ち主が終わった印は消されて False
    assert not mi.in_use_by_others("4.0.0")
    assert not (_root() / "inuse" / "4.0.0-other.lock").exists()
    assert mi.uninstall().ok


def test_acquire_use_waits_for_install_lock(net):
    assert _ok().ok
    hold, release = threading.Event(), threading.Event()

    def holder():
        with exclusive_lock(_root() / "install.lock"):
            hold.set()
            release.wait(10)
    t = threading.Thread(target=holder)
    t.start()
    assert hold.wait(5)
    got = {}
    t2 = threading.Thread(target=lambda: got.setdefault("h", mi.acquire_use(V, "mathjax-dejavu")))
    t2.start()
    time.sleep(0.3)
    assert "h" not in got
    release.set()
    t2.join(5)
    t.join(5)
    assert got["h"] is not None
    got["h"].release()
    got["h"].release()                                 # 2 回呼んでもよい


def test_acquire_use_after_uninstall_and_uninstall_while_used(net):
    assert _ok().ok
    h = mi.acquire_use(V, "mathjax-dejavu")
    assert h is not None and h.path.exists()
    r = mi.uninstall()                                  # 印を持たない側から
    assert not r.ok and r.error == "in_use"
    assert (_root() / V).exists()
    assert not mi.in_use_by_others(V, h)                # 自分の印は名前で除く
    h.release()
    assert not h.path.exists()
    assert mi.uninstall().ok
    assert mi.acquire_use(V, "mathjax-dejavu") is None
    assert list((_root() / "inuse").glob(f"{V}-*.lock")) == []
    assert (_root() / "install.lock").exists() and (_root() / "inuse").is_dir()


# ---------------------------------------------------------------- 作業ディレクトリの掃除

def _fake_workdir(token: str) -> Path:
    d = _root() / f"{V}.tmp-{token}"
    (d / "dl").mkdir(parents=True)
    return d


@pytest.mark.parametrize("op", ["install", "uninstall"])
def test_workdir_cleanup(net, op):
    _root().mkdir(parents=True, exist_ok=True)
    alive = _fake_workdir("alive")
    fd = _hold(_root() / "inuse" / "install-alive.lock")
    dead = _fake_workdir("dead")
    (_root() / "inuse" / "install-dead.lock").write_text("")
    orphan = _fake_workdir("orphan")
    try:
        assert (_ok() if op == "install" else mi.uninstall()).ok
        assert alive.exists()
        assert not dead.exists() and not (_root() / "inuse" / "install-dead.lock").exists()
        assert not orphan.exists()
    finally:
        os.close(fd)


# ---------------------------------------------------------------- 削除と導入の競合

def _run_install(font="mathjax-dejavu"):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", _ok(font)))
    t.start()
    return t, out


def test_uninstall_during_download_supersedes(net):
    net["gate"], net["reached"] = threading.Event(), threading.Event()
    t, out = _run_install()
    assert net["reached"].wait(5)
    assert mi.uninstall().ok
    assert not (_root() / V).exists()
    assert len(_workdirs()) == 1                        # 持ち主の生きている作業ディレクトリは残る
    net["gate"].set()
    t.join(10)
    assert out["r"].error == "superseded"
    assert not (_root() / V).exists()
    assert _workdirs() == [] and _install_marks() == []


def test_uninstall_during_extract_supersedes(net, monkeypatch):
    reached, gate = threading.Event(), threading.Event()
    real = mi._extract

    def slow(*a, **k):
        reached.set()
        assert gate.wait(10)
        return real(*a, **k)
    monkeypatch.setattr(mi, "_extract", slow)
    t, out = _run_install()
    assert reached.wait(5)
    assert mi.uninstall().ok
    assert len(_workdirs()) == 1
    gate.set()
    t.join(10)
    assert out["r"].error == "superseded"
    assert not (_root() / V).exists()
    assert _workdirs() == [] and _install_marks() == []


def test_uninstall_during_font_add_supersedes(net):
    assert _ok("mathjax-dejavu").ok
    net["gate"], net["reached"] = threading.Event(), threading.Event()
    t, out = _run_install("mathjax-fira")
    assert net["reached"].wait(5)
    assert mi.uninstall().ok
    net["gate"].set()
    t.join(10)
    assert out["r"].error == "superseded"
    assert not (_root() / V).exists()
    assert not (_root() / V / "fonts" / "mathjax-fira").exists()


def test_install_after_uninstall_succeeds(net):
    assert _ok().ok
    assert mi.uninstall().ok
    assert _ok().ok
    assert mi.status().state == "installed"


def test_progress_reports_bytes(net):
    seen = []
    assert mi.install("mathjax-dejavu", lambda d, t: seen.append((d, t)), lambda: False).ok
    assert seen and seen[-1][0] == len(net["blobs"][mi.MATHJAX_PACKAGE]) + \
        len(net["blobs"][mi.FONT_PACKAGES["mathjax-dejavu"]])
