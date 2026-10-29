"""数式描画モジュール（MathJax）の導入・削除・使用中の印（Qt 非依存。Issue #123）。

MathJax はリポジトリに同梱しない。設定 → 数式の描画 から、npm レジストリの tarball を取得し、
コードに固定した sha512 と照合してから PC ローカルの ``install_root()``（``~/.myanalysis/mathjax``）
へ展開する。文章は返さない（状態とエラーの種類と元のメッセージだけ。文は GUI が ``tr()`` で作る）。

``<root>`` の中身は次の 5 種類だけ:

- ``<version>/``: 版のディレクトリ（``installed.json``・``tex-svg.js``・``fonts/<フォント名>/`` …）。
  ``installed.json`` は最後に書くので、それが無いディレクトリは壊れている扱い。
- ``<version>.tmp-<token>/``: 導入 1 回ごとの作業ディレクトリ（``dl/`` と ``out/``）。持ち主は
  ``inuse/install-<token>.lock`` の OS のロックを持つ導入。
- ``generation``: 削除の世代（uuid4 の hex）。``uninstall`` が書き換える。誰も消さない。
- ``install.lock``: 導入・削除・使用の開始を直列にするロック。誰も消さない。
- ``inuse/<version>-<token>.lock``: 使用中の印（OS のロックを持ったまま開いておく）。

呼び出し側（GUI・エンジン・テスト）は ``mathjax_install.install_root()`` と属性で引くこと
（``from … import install_root`` だと conftest の差し替えが効かない）。
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import shutil
import sys
import tarfile
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from common.filelock import exclusive_lock
from common.paths import atomic_write_text

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

# ---------------------------------------------------------------- 定数（版・出所・見た目）

VERSION = "4.1.3"


@dataclass(frozen=True)
class Package:
    name: str              # npm のパッケージ名
    version: str
    tarball: str           # 実際にダウンロードする URL
    integrity: str         # sha512（npm の integrity 形式）。レジストリの申告ではなくこれと照合する
    download_size: int     # tarball のバイト数
    files: int             # 取り出すファイルの数
    extracted_size: int    # 取り出す分のバイト数

    @property
    def npm_page(self) -> str:
        return f"https://www.npmjs.com/package/{self.name}/v/{self.version}"


# 版を上げるときは、版と integrity（と大きさ）を書き換えるコミットにする。
PINNED: dict[str, Package] = {
    "mathjax": Package(
        "mathjax", "4.1.3",
        "https://registry.npmjs.org/mathjax/-/mathjax-4.1.3.tgz",
        "sha512-BN/8Pkgn7G1pIDYJqd9md+JHsE/jydSYbyOZnSdSA0WziuVO8mRxdYiWFumkVVly/8U+hm9DpIIoWuvySverzw==",
        5_257_110, 87, 15_287_294),
    "@mathjax/mathjax-dejavu-font": Package(
        "@mathjax/mathjax-dejavu-font", "4.1.3",
        "https://registry.npmjs.org/@mathjax/mathjax-dejavu-font/-/mathjax-dejavu-font-4.1.3.tgz",
        "sha512-Li3e0HYPPAhevTbiIlW97tScuIJdCy15GnC9bIowFB+mBIXjPiQlVBNVpALb7+hmKbvVCUHRNswCJhoaPNk/Ew==",
        2_073_422, 20, 1_048_276),
    "@mathjax/mathjax-fira-font": Package(
        "@mathjax/mathjax-fira-font", "4.1.3",
        "https://registry.npmjs.org/@mathjax/mathjax-fira-font/-/mathjax-fira-font-4.1.3.tgz",
        "sha512-dv+CVEEWNMe15YOKz2hzRXVii1qa1I0Jldtk6JltxazLxT926+RtIcEoa4wCUEi4qiL3Po0JkOFjKiH23kwAOQ==",
        3_867_045, 25, 2_635_584),
    "@mathjax/mathjax-stix2-font": Package(
        "@mathjax/mathjax-stix2-font", "4.1.3",
        "https://registry.npmjs.org/@mathjax/mathjax-stix2-font/-/mathjax-stix2-font-4.1.3.tgz",
        "sha512-DjJh93JuKodoYmvWawiqa22y/dwP+hn6/DN/DepFRFaRbdndNcSD3iBsBunSrLPFIJBS83mn4iR015YRPWZWhg==",
        4_829_163, 26, 3_173_659),
    "@mathjax/mathjax-newcm-font": Package(
        "@mathjax/mathjax-newcm-font", "4.1.3",
        "https://registry.npmjs.org/@mathjax/mathjax-newcm-font/-/mathjax-newcm-font-4.1.3.tgz",
        "sha512-gzAB3dFHilHX1l5x2xUqRL+1jDQt3Fyza1DkEMVXWC4E8SvsGdlgEza47HYi2WhVcgfkvf4zgUGzuhbq3Pjlew==",
        13_872_892, 41, 10_945_020),
}

# 出所（URL と固有名だけ）
MATHJAX_PACKAGE = "mathjax"
MATHJAX_DEVELOPER = "MathJax Consortium"
MATHJAX_SPONSORS = ("AMS", "SIAM")
MATHJAX_HOMEPAGE = "https://www.mathjax.org/"
MATHJAX_SOURCE = "https://github.com/mathjax/MathJax"
MATHJAX_LICENSE = "Apache-2.0"
LICENSE_URL = "https://www.apache.org/licenses/LICENSE-2.0"
FONTS_SOURCE = "https://github.com/mathjax/MathJax-fonts"

# フォント名（MathJax の output.font の値。ui_prefs math_font にもこの文字列を保存する）。並びは表示順。
FONTS = ("mathjax-fira", "mathjax-dejavu", "mathjax-stix2", "mathjax-newcm")
FONT_PACKAGES = {f: f"@mathjax/{f}-font" for f in FONTS}
FONT_LICENSES = {f: "Apache-2.0" for f in FONTS}
FONT_ORIGINS = {
    "mathjax-fira": "Fira Math",
    "mathjax-dejavu": "DejaVu",
    "mathjax-stix2": "STIX Two",
    "mathjax-newcm": "New Computer Modern",
}

# 見た目の既定（ダイアログ・gui/math_render.py・gui/math_engine.py はここから読む）
DEFAULT_FONT = "mathjax-dejavu"
SCALES = (1.0, 1.1, 1.2, 1.35)
DEFAULT_SCALE = 1.2
DEFAULT_BOLD = True
MIN_SCALE = 0.7
BOLD_STROKE = 15           # 太めの輪郭の幅（1/1000 em）

ERRORS = ("network", "integrity", "archive", "cancelled", "io", "in_use", "superseded")

FETCH_TIMEOUT_S = 30       # urllib の 1 回の読み込みごとのタイムアウト
_CHUNK = 64 * 1024


def font_package(font: str) -> Package:
    return PINNED[FONT_PACKAGES[font]]


def mathjax_package() -> Package:
    return PINNED[MATHJAX_PACKAGE]


def install_root() -> Path:
    """導入先（PC ローカル）。テストは conftest で tmp に向ける。"""
    return Path.home() / ".myanalysis" / "mathjax"


# ---------------------------------------------------------------- 結果

@dataclass(frozen=True)
class InstallResult:
    ok: bool
    error: str | None = None       # ERRORS のどれか
    message: str = ""              # 元のメッセージ（表示の文ではない）


@dataclass(frozen=True)
class Status:
    state: str                     # missing / installed / outdated / broken
    version: str | None = None
    fonts: tuple[str, ...] = ()


class _Fail(Exception):
    def __init__(self, kind: str, message: str = ""):
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------- 状態

def _version_dir(root: Path, version: str = VERSION) -> Path:
    return root / version


def _read_installed(vdir: Path) -> dict | None:
    try:
        data = json.loads((vdir / "installed.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _font_ok(vdir: Path, data: dict, font: str) -> bool:
    fonts = data.get("fonts")
    return isinstance(fonts, list) and font in fonts and (vdir / "fonts" / font / "svg.js").is_file()


def _version_complete(vdir: Path, data: dict | None) -> bool:
    return data is not None and data.get("version") == VERSION and (vdir / "tex-svg.js").is_file()


def status() -> Status:
    """入っている版とフォント。手で消された分は「入っている」と言わない。"""
    root = install_root()
    vdir = _version_dir(root)
    if vdir.is_dir():
        data = _read_installed(vdir)
        if not _version_complete(vdir, data):
            return Status("broken", VERSION)
        fonts = tuple(f for f in FONTS if _font_ok(vdir, data, f))
        return Status("installed", VERSION, fonts)
    # 版のディレクトリ（作業ディレクトリ以外）が他にあれば outdated
    try:
        others = sorted(p.name for p in root.iterdir()
                        if p.is_dir() and ".tmp-" not in p.name and p.name != "inuse")
    except OSError:
        others = []
    for name in others:
        data = _read_installed(root / name)
        if data is not None:
            return Status("outdated", str(data.get("version") or name))
    if others:
        return Status("broken", others[0])
    return Status("missing")


def is_usable(font: str) -> bool:
    st = status()
    return st.state == "installed" and font in st.fonts


def version_dir() -> Path:
    """今の版のディレクトリ（在るとは限らない）。"""
    return _version_dir(install_root())


# ---------------------------------------------------------------- 印（OS のロック）

def _try_lock(fd: int) -> bool:
    try:
        if sys.platform == "win32":
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


class _Mark:
    """OS のロックを持ったまま開いておく印のファイル。"""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fd: int | None = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        if not _try_lock(self._fd):        # 自分の新しいファイルなので取れるはず
            os.close(self._fd)
            self._fd = None
            raise OSError(f"cannot lock {path}")

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        _unlock(fd)
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            self.path.unlink()


class UseHandle:
    """使用中の印。``release()`` は何度呼んでもよい。"""

    def __init__(self, version: str, mark: _Mark):
        self.version = version
        self._mark = mark

    @property
    def path(self) -> Path:
        return self._mark.path

    def release(self) -> None:
        self._mark.release()


def _owner_gone(path: Path) -> bool:
    """印の持ち主がもういないか（ロックが取れた・ファイルが無い）。取れたら印を消す。"""
    try:
        fd = os.open(str(path), os.O_RDWR)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        if not _try_lock(fd):
            return False
        _unlock(fd)
    finally:
        os.close(fd)
    with contextlib.suppress(OSError):
        path.unlink()
    return True


def _inuse_dir(root: Path) -> Path:
    return root / "inuse"


def _lock_path(root: Path) -> Path:
    return root / "install.lock"


@contextlib.contextmanager
def _root_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    _inuse_dir(root).mkdir(exist_ok=True)
    with exclusive_lock(_lock_path(root)):
        yield


def acquire_use(version: str, font: str) -> UseHandle | None:
    """その版が完全で font が入っていれば使用中の印を作って返す。でなければ None。"""
    root = install_root()
    with _root_lock(root):
        vdir = _version_dir(root, version)
        data = _read_installed(vdir)
        if version != VERSION or not _version_complete(vdir, data) or not _font_ok(vdir, data, font):
            return None
        path = _inuse_dir(root) / f"{version}-{uuid.uuid4().hex}.lock"
        return UseHandle(version, _Mark(path))


def in_use_by_others(version: str, own: UseHandle | None = None) -> bool:
    root = install_root()
    d = _inuse_dir(root)
    try:
        marks = sorted(d.glob(f"{version}-*.lock"))
    except OSError:
        return False
    busy = False
    for p in marks:
        if own is not None and p == own.path:
            continue
        if not _owner_gone(p):
            busy = True
    return busy


# ---------------------------------------------------------------- 取得・展開

def _urlopen(url: str, timeout: float):
    """テストが差し替える取得口。プロキシは urllib の既定（環境変数・Windows のレジストリ）。"""
    return urllib.request.urlopen(url, timeout=timeout)  # noqa: S310 — https の固定 URL だけ


def _integrity_of(h: "hashlib._Hash") -> str:
    return "sha512-" + base64.b64encode(h.digest()).decode("ascii")


def _download(pkg: Package, dest: Path, done: list[int], total: int,
              progress: Callable[[int, int], None], cancelled: Callable[[], bool]) -> None:
    """一時ファイルに流しながら sha512 を計算し、固定値と照合する。"""
    h = hashlib.sha512()
    try:
        f = open(dest, "wb")
    except OSError as e:
        raise _Fail("io", f"{type(e).__name__}: {e}") from e
    with f:
        try:
            resp = _urlopen(pkg.tarball, FETCH_TIMEOUT_S)
        except OSError as e:      # urllib.error.URLError・タイムアウトは OSError のサブクラス
            raise _Fail("network", f"{type(e).__name__}: {e}") from e
        with resp:
            while True:
                if cancelled():
                    raise _Fail("cancelled")
                try:
                    chunk = resp.read(_CHUNK)
                except OSError as e:
                    raise _Fail("network", f"{type(e).__name__}: {e}") from e
                if not chunk:
                    break
                h.update(chunk)
                try:
                    f.write(chunk)
                except OSError as e:
                    raise _Fail("io", f"{type(e).__name__}: {e}") from e
                done[0] += len(chunk)
                progress(done[0], total)
    if _integrity_of(h) != pkg.integrity:
        raise _Fail("integrity", f"{pkg.name}: {_integrity_of(h)}")


def _member_parts(m: tarfile.TarInfo) -> tuple[str, ...]:
    name = m.name
    if name.startswith("/") or "\\" in name or (len(name) > 1 and name[1] == ":"):
        raise _Fail("archive", f"absolute path: {name}")
    parts = PurePosixPath(name).parts
    if not parts or parts[0] != "package" or any(p in ("..", "") for p in parts):
        raise _Fail("archive", f"outside package/: {name}")
    if not (m.isfile() or m.isdir()):
        raise _Fail("archive", f"not a regular file: {name}")
    return parts


def _wanted_mathjax(parts: tuple[str, ...]) -> tuple[str, ...] | None:
    rel = parts[1:]
    if not rel or rel[0] == "sre":
        return None
    return rel


def _wanted_font(font: str):
    def pick(parts: tuple[str, ...]) -> tuple[str, ...] | None:
        rel = parts[1:]
        if rel == ("svg.js",) or (len(rel) > 1 and rel[0] == "svg"):
            return ("fonts", font) + rel
        return None
    return pick


def _extract(tgz: Path, out: Path, pick, cancelled: Callable[[], bool]) -> None:
    try:
        with tarfile.open(tgz, "r:gz") as tar:
            members = tar.getmembers()
            checked = [(m, _member_parts(m)) for m in members]   # 全エントリを先に検査する
            data_filter = getattr(tarfile, "data_filter", None)
            for m, parts in checked:
                if cancelled():
                    raise _Fail("cancelled")
                if not m.isfile():
                    continue
                rel = pick(parts)
                if rel is None:
                    continue
                if data_filter is not None:
                    try:
                        data_filter(m, str(out))
                    except tarfile.FilterError as e:
                        raise _Fail("archive", str(e)) from e
                dest = out.joinpath(*rel)
                dest.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(m)
                if src is None:
                    raise _Fail("archive", f"unreadable: {m.name}")
                with src, open(dest, "wb") as f:
                    shutil.copyfileobj(src, f)
    except _Fail:
        raise
    except (tarfile.TarError, EOFError, ValueError) as e:
        raise _Fail("archive", f"{type(e).__name__}: {e}") from e


# ---------------------------------------------------------------- 掃除

def _rmtree(p: Path) -> None:
    """消せるだけ消す（失敗は握る）。消えたかは呼び出し側が ``exists()`` で確かめる。"""
    shutil.rmtree(p, ignore_errors=True)


def _read_generation(root: Path) -> str:
    try:
        return (root / "generation").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _clean_workdirs(root: Path, own_token: str | None) -> None:
    """持ち主のいない作業ディレクトリを消す（install.lock を持ったまま呼ぶ）。"""
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    for p in entries:
        if not p.is_dir() or ".tmp-" not in p.name:
            continue
        token = p.name.split(".tmp-", 1)[1]
        if token == own_token:
            continue
        mark = _inuse_dir(root) / f"install-{token}.lock"
        if _owner_gone(mark):
            _rmtree(p)


def _clean_old_versions(root: Path) -> None:
    """今の版以外の版のディレクトリを消す（使用中は残す。install.lock を持ったまま呼ぶ）。"""
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    for p in entries:
        if not p.is_dir() or ".tmp-" in p.name or p.name in ("inuse", VERSION):
            continue
        if not in_use_by_others(p.name):
            _rmtree(p)


# ---------------------------------------------------------------- 導入・削除

def install(font: str, progress: Callable[[int, int], None],
            cancelled: Callable[[], bool]) -> InstallResult:
    """mathjax と font を取得・照合・展開して導入する（モジュールの説明と Issue #123 §2）。never raise。"""
    if font not in FONTS:
        return InstallResult(False, "io", f"unknown font: {font}")
    root = install_root()
    token = uuid.uuid4().hex
    work = root / f"{VERSION}.tmp-{token}"
    mark: _Mark | None = None
    try:
        # 0. 世代を覚え、作業ディレクトリと持ち主の印を作る
        with _root_lock(root):
            g0 = _read_generation(root)
            mark = _Mark(_inuse_dir(root) / f"install-{token}.lock")
            (work / "dl").mkdir(parents=True)
            (work / "out").mkdir()
        mj, fp = mathjax_package(), font_package(font)
        total = mj.download_size + fp.download_size
        done = [0]
        # 1. 取得（ロックの外）
        dl_mj, dl_font = work / "dl" / "mathjax.tgz", work / "dl" / "font.tgz"
        _download(mj, dl_mj, done, total, progress, cancelled)
        _download(fp, dl_font, done, total, progress, cancelled)
        # 2. 展開（ロックの外。作業ディレクトリの中だけ）
        out = work / "out"
        _extract(dl_mj, out, _wanted_mathjax, cancelled)
        _extract(dl_font, out, _wanted_font(font), cancelled)
        if cancelled():
            raise _Fail("cancelled")
        # 3〜4. 置く（短い区間だけロック）
        with _root_lock(root):
            if _read_generation(root) != g0:
                raise _Fail("superseded")
            _place(root, out, font, mj, fp)
            _clean_old_versions(root)
            _clean_workdirs(root, token)
        return InstallResult(True)
    except _Fail as e:
        return InstallResult(False, e.kind, str(e))
    except OSError as e:
        return InstallResult(False, "io", f"{type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001 — never raise
        return InstallResult(False, "io", f"{type(e).__name__}: {e}")
    finally:
        # 5. 自分の作業ディレクトリと印を片付ける
        _rmtree(work)
        if mark is not None:
            mark.release()


def _place(root: Path, out: Path, font: str, mj: Package, fp: Package) -> None:
    vdir = _version_dir(root)
    data = _read_installed(vdir) if vdir.exists() else None
    if _version_complete(vdir, data):
        if _font_ok(vdir, data, font):
            return                                              # 3-1. もう入っている
        fdir = vdir / "fonts" / font                            # 3-2. フォントだけ足す
        if fdir.exists():
            _rmtree(fdir)
        fdir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(out / "fonts" / font, fdir)
        fonts = [f for f in data.get("fonts", []) if isinstance(f, str)]
        if font not in fonts:
            fonts.append(font)
        integ = dict(data.get("integrity") or {})
        integ[fp.name] = fp.integrity
        new = dict(data, fonts=fonts, integrity=integ)
        atomic_write_text(vdir / "installed.json", json.dumps(new, ensure_ascii=False, indent=2))
        return
    if vdir.exists():                                           # 3-3. 壊れた版を置き換える
        if in_use_by_others(VERSION):
            raise _Fail("in_use")
        _rmtree(vdir)
        if vdir.exists():
            raise _Fail("io", f"cannot remove {vdir}")
    os.replace(out, vdir)
    info = {"version": VERSION, "fonts": [font],
            "integrity": {mj.name: mj.integrity, fp.name: fp.integrity}}
    atomic_write_text(vdir / "installed.json", json.dumps(info, ensure_ascii=False, indent=2))


def uninstall() -> InstallResult:
    """版のディレクトリを全部消す（使っている GUI があれば何も変えずに in_use）。never raise。"""
    root = install_root()
    try:
        with _root_lock(root):
            versions = [p for p in root.iterdir()
                        if p.is_dir() and ".tmp-" not in p.name and p.name != "inuse"]
            if any(in_use_by_others(p.name) for p in versions):
                return InstallResult(False, "in_use")
            atomic_write_text(root / "generation", uuid.uuid4().hex)
            for p in versions:
                _rmtree(p)
            _clean_workdirs(root, None)
            left = [p.name for p in versions if p.exists()]
        if left:        # 占有・アクセス拒否などで消し切れなかった（rmtree は失敗を握る）
            return InstallResult(False, "io", "cannot remove: " + ", ".join(left))
        return InstallResult(True)
    except OSError as e:
        return InstallResult(False, "io", f"{type(e).__name__}: {e}")
