"""git の読み取りと実行（Qt 非依存。標準ライブラリと common.proc だけを使う）。

ヘルプ → MyAnalysis を更新（``common.self_update``）とバージョン情報（``gui.about_dialog``）が使う。

- 起動時の HEAD（``read_head``）・今のブランチ（``current_branch``）・コミットの日時
  （``read_commit_time``）は、git を実行せず ``.git`` のファイルを読む。起動時に git は動かさない。
- ``run_git`` は git を 1 回実行する。例外は投げず、失敗も ``CommandResult(ok=False, ...)`` で返す。
  認証は聞かない（``GIT_TERMINAL_PROMPT=0`` / ``GCM_INTERACTIVE=never``、askpass を外す）。
  リポジトリの場所を変える ``GIT_*`` を外し、``GIT_CEILING_DIRECTORIES`` を親フォルダにして
  外側のリポジトリを探さない。時間切れは ``common.proc.run_capture`` がツリーごと止める。
- 文章は返さない（状態・終了コード・git の出力だけ）。表示の文は GUI が ``tr()`` で作る。

``read_head`` / ``read_commit_time`` は pjdeck の ``common/git.py`` から移したもの。
"""
from __future__ import annotations

import os
import re
import shutil
import struct
import zlib
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from common.proc import CommandResult, run_capture

# 子に渡さない環境変数。リポジトリの場所を変えるもの（このリポジトリ以外を触らない）と、
# 認証を聞く画面を出すもの（VS Code の端末から起動すると GIT_ASKPASS が VS Code を指している）。
_DROP_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
    "GIT_ASKPASS", "SSH_ASKPASS",
)


def find_git() -> str | None:
    """PATH の git（無い・調べられなければ None）。"""
    try:
        return shutil.which("git")
    except Exception:  # noqa: BLE001 — 探せなければ「無い」
        return None


def git_env(repo) -> dict[str, str]:
    """run_git が子に渡す環境変数。"""
    env = dict(os.environ)
    for name in _DROP_ENV:
        env.pop(name, None)
    env["GIT_TERMINAL_PROMPT"] = "0"    # ターミナルで認証を聞いて止まらない
    env["GCM_INTERACTIVE"] = "never"    # Git Credential Manager の画面を出さない
    env["GIT_MERGE_AUTOEDIT"] = "no"    # マージでエディタを開かない
    # 親フォルダより上でリポジトリを探さない（.git が消えていても外側を触らない）
    env["GIT_CEILING_DIRECTORIES"] = os.path.dirname(os.path.abspath(repo))
    return env


def run_git(repo, args: list[str], timeout: float) -> CommandResult:
    """repo で ``git <args>`` を実行する。never raise。"""
    try:
        if not Path(repo).is_dir():
            return CommandResult(False, f"not a directory: {repo}")
        git = find_git()
        if git is None:
            return CommandResult(False, "git not found")
        return run_capture([git, *args], cwd=repo, env=git_env(repo), timeout=timeout)
    except Exception as e:  # noqa: BLE001 — 何があっても結果で返す
        return CommandResult(False, f"{type(e).__name__}: {e}")


def clip_output(text: str, max_lines: int = 40, marker: str = "… ({n} more lines) …") -> str:
    """長い出力を、省略の行も含めて max_lines 行以内に縮める。残す原文は max_lines - 1 行で、
    その 1/4（切り捨て）を先頭から、残りを末尾から取り、間に ``marker.format(n=省いた行数)`` を
    1 行入れる（git は失敗の理由と対処のコマンドを最後に出すので、末尾を多く残す）。
    max_lines 行以内なら、そのまま返す。max_lines が 2 未満なら ValueError。"""
    if max_lines < 2:
        raise ValueError(f"max_lines must be >= 2: {max_lines}")
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    keep = max_lines - 1
    head = keep // 4
    tail = keep - head               # keep >= 1 なので tail >= 1（lines[-0:] で全行にならない）
    return "\n".join(lines[:head] + [marker.format(n=len(lines) - keep)] + lines[-tail:])


def current_branch(repo) -> str | None:
    """repo の今のブランチ名（``HEAD`` が ``ref: refs/heads/<name>`` のときの name）。
    detached・読めない・git のリポジトリでないときは None。never raise。"""
    try:
        dirs = _git_dirs(Path(repo))
        if dirs is None:
            return None
        head = (dirs[0] / "HEAD").read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        return None
    prefix = "ref: refs/heads/"
    if not head.startswith(prefix):
        return None
    return head[len(prefix):].strip() or None


_HEX_DIGITS = frozenset("0123456789abcdef")


def read_head(repo) -> str | None:
    """repo の今のコミット id（小文字の 16 進数 40 桁か 64 桁）。git は実行せず ``.git`` のファイルを読む。

    ``.git`` がファイル（``gitdir: …``。相対なら ``.git`` のあるフォルダから解く）でもよく、その gitdir に
    ``commondir`` があれば（worktree）、``HEAD`` は gitdir から、ref（loose と ``packed-refs``）は
    commondir の先から読む。``HEAD`` が ``ref: …`` なら 1 段だけ辿る（loose → ``packed-refs`` の順）。
    読めない・git のリポジトリでない・中身がコミット id でないときは None。never raise。
    """
    try:
        return _read_head(Path(repo))
    except Exception:  # noqa: BLE001 — 読めなければ「分からない」
        return None


def _commit_id(text: str) -> str | None:
    value = text.strip().lower()
    if len(value) in (40, 64) and all(c in _HEX_DIGITS for c in value):
        return value
    return None


def _git_dirs(repo: Path) -> tuple[Path, Path] | None:
    """repo の (gitdir, commondir)。commondir は ref とオブジェクトの置き場で、worktree でなければ gitdir。
    ``.git`` がファイルで ``gitdir: …`` で始まらなければ None。読めなければ例外。"""
    dotgit = repo / ".git"
    if dotgit.is_dir():
        gitdir = dotgit
    else:
        first = dotgit.read_text(encoding="utf-8").splitlines()
        if not first or not first[0].startswith("gitdir:"):
            return None
        gitdir = Path(first[0][len("gitdir:"):].strip())
        if not gitdir.is_absolute():
            gitdir = repo / gitdir
    common = gitdir
    try:
        commondir = (gitdir / "commondir").read_text(encoding="utf-8").strip()
    except (FileNotFoundError, NotADirectoryError):
        commondir = ""
    if commondir:
        common = Path(commondir)
        if not common.is_absolute():
            common = gitdir / common
    return gitdir, common


def _read_head(repo: Path) -> str | None:
    dirs = _git_dirs(repo)
    if dirs is None:
        return None
    gitdir, common = dirs
    head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
    if not head.startswith("ref:"):
        return _commit_id(head)
    value = _read_ref(common, head[len("ref:"):].strip())
    if value is None or value.startswith("ref:"):
        return None
    return _commit_id(value)


def _read_ref(common: Path, ref: str) -> str | None:
    """ref の中身（loose のファイル、無ければ packed-refs の行）。見つからなければ None。"""
    if not ref:
        return None
    try:
        return (common / ref).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        pass
    try:
        lines = (common / "packed-refs").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    for line in lines:
        if not line or line.startswith(("#", "^")):
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[1].strip() == ref:
            return parts[0].strip()
    return None


# ----- コミットの日時（オブジェクトのファイルを読む。git は呼ばない）-----

_MAX_OBJECT = 1 << 20          # 組み立てた中身・delta 1 つの大きさの上限（壊れた pack で大量のメモリを使わない）
_MAX_CHAIN_BYTES = 16 << 20    # 基まで辿る間に持つ delta の合計の上限
_MAX_DELTA_DEPTH = 1000        # 辿る delta の段数の上限（git の pack.depth の既定は 50）
_LOOSE_KINDS = {b"commit": 1, b"tree": 2, b"blob": 3, b"tag": 4}
_COMMIT = 1
_OFS_DELTA = 6
_REF_DELTA = 7
_IDX_MAGIC = b"\xfftOc"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_TZ_RE = re.compile(rb"[+-]\d{4}")

# 読めない pack を飛ばして次を見るときに拾う例外（壊れた中身・gc の最中に消えたファイル・版の違い）。
_PACK_ERRORS = (OSError, ValueError, IndexError, zlib.error, struct.error)

# loose・pack の 1 件: (種類, 中身, 基, pack)。delta なら中身は delta で、基は OFS なら同じ pack の位置（int）、
# REF なら基の id（str）。基のオブジェクトなら基は None。loose なら pack も None。
_Entry = tuple[int, bytes, int | str | None, Path | None]


def read_commit_time(repo, commit: str | None) -> datetime | None:
    """repo のコミット commit（``read_head`` の値）の committer の日時。コミットに記録された時差の aware な
    datetime で返す（``git log --date=format:…`` と同じ日時）。

    git は実行せず ``objects`` のファイルを読む（worktree でも commondir 側）。loose を先に、無ければ
    ``pack/pack-*.idx``（v2 だけ。ほかの版・読めない pack は飛ばす）から探し、delta（OFS / REF）は基まで
    辿って組み立てる。alternates は見ず、チェックサムと CRC は確かめない（表示に使うだけ）。
    無い・壊れている・大きすぎる・commit でない・時刻の形が違うときは None。never raise。
    """
    try:
        oid = _commit_id(commit) if isinstance(commit, str) else None
        if oid is None:
            return None
        dirs = _git_dirs(Path(repo))
        if dirs is None:
            return None
        obj = _read_object(dirs[1] / "objects", oid)
        if obj is None or obj[0] != _COMMIT:
            return None
        return _committer_time(obj[1])
    except Exception:  # noqa: BLE001 — 読めなければ「分からない」
        return None


def _read_object(objects: Path, oid: str) -> tuple[int, bytes] | None:
    """オブジェクト oid の (種類, 中身)。無い・辿る段や delta が多すぎるときは None。delta は基までの鎖を
    先に集めてから、基の側から当てる。"""
    hash_len = len(oid) // 2
    deltas: list[bytes] = []
    held = 0
    entry = _find_entry(objects, oid, hash_len)
    while entry is not None:
        kind, data, base, pack = entry
        if base is None:
            for delta in reversed(deltas):
                data = _apply_delta(data, delta)
            return kind, data
        held += len(data)
        if len(deltas) >= _MAX_DELTA_DEPTH or held > _MAX_CHAIN_BYTES:
            return None
        deltas.append(data)
        if isinstance(base, int):
            entry = _pack_entry(pack, base, hash_len)
        else:
            entry = _find_entry(objects, base, hash_len)
    return None


def _find_entry(objects: Path, oid: str, hash_len: int) -> _Entry | None:
    """oid の 1 件（``_Entry``）。無ければ None。loose が無ければ pack を名前の順に見て、読めない pack は
    飛ばす。"""
    try:
        with (objects / oid[:2] / oid[2:]).open("rb") as f:
            raw = _inflate(f, _MAX_OBJECT + 64)
    except (FileNotFoundError, NotADirectoryError):
        raw = None
    if raw is not None:
        kind, data = _parse_loose(raw)
        return kind, data, None, None
    name = bytes.fromhex(oid)
    for idx in sorted((objects / "pack").glob("pack-*.idx")):
        try:
            offset = _idx_offset(idx, name)
            if offset is not None:
                return _pack_entry(idx.with_suffix(".pack"), offset, hash_len)
        except _PACK_ERRORS:
            continue
    return None


def _parse_loose(raw: bytes) -> tuple[int, bytes]:
    """loose を展開したもの（``<種類> <大きさ>\\0<中身>``）の (種類, 中身)。形が違えば ValueError。"""
    nul = raw.find(b"\0", 0, 32)
    if nul < 0:
        raise ValueError("bad loose object")
    kind, _, size = raw[:nul].partition(b" ")
    if kind not in _LOOSE_KINDS or not size.isdigit() or int(size) != len(raw) - nul - 1:
        raise ValueError("bad loose object")
    return _LOOSE_KINDS[kind], raw[nul + 1:]


def _idx_offset(path: Path, name: bytes) -> int | None:
    """idx v2 の path で name の pack の中の位置を引く（無ければ None）。丸ごとは読まず、fanout で絞った
    範囲の名前だけを読んで二分探索する。v2 でない・形が違うときは ValueError。"""
    n = len(name)
    with path.open("rb") as f:
        head = f.read(8 + 1024)
        if len(head) != 8 + 1024 or head[:4] != _IDX_MAGIC or struct.unpack(">I", head[4:8])[0] != 2:
            raise ValueError("unsupported idx")
        fanout = struct.unpack(">256I", head[8:])
        total = fanout[255]
        lo = fanout[name[0] - 1] if name[0] else 0
        hi = fanout[name[0]]
        if not lo <= hi <= total:
            raise ValueError("bad fanout")
        f.seek(8 + 1024 + lo * n)
        names = f.read((hi - lo) * n)
        if len(names) != (hi - lo) * n:
            raise ValueError("truncated idx")
        a, b = 0, hi - lo
        while a < b:
            m = (a + b) // 2
            if names[m * n:(m + 1) * n] < name:
                a = m + 1
            else:
                b = m
        if a == hi - lo or names[a * n:(a + 1) * n] != name:
            return None
        offsets = 8 + 1024 + total * n + total * 4      # 名前と CRC の後
        f.seek(offsets + (lo + a) * 4)
        (offset,) = struct.unpack(">I", f.read(4))
        if offset & 0x80000000:                          # 8 バイトの表の何番目か
            f.seek(offsets + total * 4 + (offset & 0x7FFFFFFF) * 8)
            (offset,) = struct.unpack(">Q", f.read(8))
        return offset


def _pack_entry(pack: Path, offset: int, hash_len: int) -> _Entry:
    """pack の offset の 1 件（``_Entry``）。形が違う・大きすぎるときは ValueError。"""
    with pack.open("rb") as f:
        f.seek(offset)
        c = _byte(f)
        kind = (c >> 4) & 7
        size = c & 0x0F
        shift = 4
        while c & 0x80:
            c = _byte(f)
            size |= (c & 0x7F) << shift
            shift += 7
            if shift > 64:
                raise ValueError("bad size")
        if size > _MAX_OBJECT:
            raise ValueError("too large")
        base: int | str | None = None
        if kind == _OFS_DELTA:
            c = _byte(f)
            rel = c & 0x7F
            while c & 0x80 and rel <= offset:
                c = _byte(f)
                rel = ((rel + 1) << 7) | (c & 0x7F)
            if not 0 < rel <= offset:
                raise ValueError("bad delta base")
            base = offset - rel
        elif kind == _REF_DELTA:
            raw = f.read(hash_len)
            if len(raw) != hash_len:
                raise ValueError("truncated pack")
            base = raw.hex()
        elif kind not in _LOOSE_KINDS.values():
            raise ValueError("bad kind")
        data = _inflate(f, size)
        if len(data) != size:
            raise ValueError("size mismatch")
        return kind, data, base, pack


def _byte(f) -> int:
    b = f.read(1)
    if not b:
        raise ValueError("truncated pack")
    return b[0]


def _inflate(f, limit: int) -> bytes:
    """f の今の位置からの zlib を展開する。limit バイトを超える・途中で切れているときは ValueError。"""
    d = zlib.decompressobj()
    out = bytearray()
    while not d.eof:
        chunk = d.unconsumed_tail or f.read(16384)
        if not chunk:
            raise ValueError("truncated zlib")
        out += d.decompress(chunk, limit + 1 - len(out))
        if len(out) > limit:
            raise ValueError("too large")
    return bytes(out)


def _delta_size(delta: bytes, pos: int) -> tuple[int, int]:
    """delta の見出しの大きさ（7 ビットずつ下から）。(次の位置, 大きさ)。"""
    value = shift = 0
    while True:
        c = delta[pos]
        pos += 1
        value |= (c & 0x7F) << shift
        shift += 7
        if not c & 0x80:
            return pos, value
        if shift > 64:
            raise ValueError("bad delta size")


def _apply_delta(base: bytes, delta: bytes) -> bytes:
    """基 base に delta（copy / insert の命令）を当てる。形が違う・大きすぎるときは ValueError。"""
    pos, src_size = _delta_size(delta, 0)
    pos, dst_size = _delta_size(delta, pos)
    if src_size != len(base) or dst_size > _MAX_OBJECT:
        raise ValueError("bad delta")
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:                                   # copy: 基の (位置, 大きさ) を写す
            start = size = 0
            for i in range(4):
                if op & (1 << i):
                    start |= delta[pos] << (8 * i)
                    pos += 1
            for i in range(3):
                if op & (0x10 << i):
                    size |= delta[pos] << (8 * i)
                    pos += 1
            if size == 0:
                size = 0x10000
            if start + size > len(base):
                raise ValueError("bad copy")
            out += base[start:start + size]
        elif op:                                        # insert: 続く op バイトをそのまま
            if pos + op > len(delta):
                raise ValueError("bad insert")
            out += delta[pos:pos + op]
            pos += op
        else:
            raise ValueError("reserved delta op")
        if len(out) > dst_size:
            raise ValueError("delta too long")
    if len(out) != dst_size:
        raise ValueError("delta size mismatch")
    return bytes(out)


def _committer_time(data: bytes) -> datetime | None:
    """コミットの見出し（最初の空行まで）の committer の行の日時。無い・形が違うときは None。"""
    for line in data.split(b"\n"):
        if not line:
            return None
        if line.startswith(b"committer "):
            fields = line[line.rfind(b">") + 1:].split()
            if len(fields) != 2 or not fields[0].isdigit() or not _TZ_RE.fullmatch(fields[1]):
                return None
            hours, minutes = int(fields[1][1:3]), int(fields[1][3:5])
            if hours >= 24 or minutes >= 60:
                return None
            offset = timedelta(hours=hours, minutes=minutes)
            if fields[1][:1] == b"-":
                offset = -offset
            return (_EPOCH + timedelta(seconds=int(fields[0]))).astimezone(timezone(offset))
    return None
