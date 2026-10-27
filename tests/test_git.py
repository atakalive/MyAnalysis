"""common.git（起動時の HEAD とその日時・今のブランチ・git の実行）と common.proc.run_capture。

read_head / read_commit_time のテストは pjdeck の tests/test_git.py から移したもの。
"""
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import zlib
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from common import git as G
from common import proc as P
from common.git import clip_output, current_branch, read_commit_time, read_head, run_git
from common.proc import run_capture

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git が無い")


def run(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   cwd=cwd, check=True, capture_output=True)


def isolate_git_env(monkeypatch, config) -> None:
    """外から来た GIT_* を全部外してから、git の設定を空のファイル config だけにし、メッセージを
    英語にする。GIT_DIR / GIT_WORK_TREE が残っていると cwd に関係なく利用者のリポジトリを触り、
    GIT_CONFIG_COUNT などが残っていると設定が混ざるため。"""
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.delenv("LANGUAGE", raising=False)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """本物の git を打つテストの環境（isolate_git_env）。tmp_path を返す。"""
    config = tmp_path / "gitconfig"
    config.write_text("")
    isolate_git_env(monkeypatch, config)
    return tmp_path


# ----- run_git / run_capture -----

_DROPPED = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_ASKPASS", "SSH_ASKPASS",
            "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY")


def test_git_env_drops_location_and_askpass(tmp_path, monkeypatch):
    for name in _DROPPED:
        monkeypatch.setenv(name, str(tmp_path / "elsewhere"))
    env = G.git_env(tmp_path / "repo")
    assert not set(_DROPPED) & set(env)
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GCM_INTERACTIVE"] == "never"
    assert env["GIT_CEILING_DIRECTORIES"] == str(tmp_path)


@needs_git
def test_run_git_ignores_git_dir_from_outside(isolated, monkeypatch):
    run(isolated, "init", "-q", "mine")
    run(isolated, "init", "-q", "other")
    monkeypatch.setenv("GIT_DIR", str(isolated / "other" / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(isolated / "other"))
    res = run_git(isolated / "mine", ["rev-parse", "--absolute-git-dir"], 30)
    assert res.ok, res.output
    assert os.path.samefile(res.output.strip(), isolated / "mine" / ".git")


@needs_git
def test_run_git_does_not_use_outer_repo(isolated):
    run(isolated, "init", "-q", "outer")
    sub = isolated / "outer" / "sub"
    sub.mkdir()
    res = run_git(sub, ["rev-parse", "--git-dir"], 30)
    assert not res.ok and res.returncode not in (0, None)


def test_run_git_missing_folder_and_no_git(tmp_path, monkeypatch):
    res = run_git(tmp_path / "nope", ["status"], 30)
    assert not res.ok and res.returncode is None
    monkeypatch.setattr(G, "find_git", lambda: None)
    res = run_git(tmp_path, ["status"], 30)
    assert not res.ok and res.returncode is None


def test_run_git_never_raises(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(G, "run_capture", boom)
    monkeypatch.setattr(G, "find_git", lambda: "git")
    res = run_git(tmp_path, ["status"], 30)
    assert not res.ok and "boom" in res.output


def test_run_capture_output_and_returncode(tmp_path):
    code = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
    res = run_capture([sys.executable, "-c", code], cwd=tmp_path, env=None, timeout=30)
    assert not res.ok and res.returncode == 3
    assert "out" in res.output and "err" in res.output


def test_run_capture_cannot_start(tmp_path):
    res = run_capture([str(tmp_path / "no-such-program")], cwd=tmp_path, env=None, timeout=30)
    assert not res.ok and res.returncode is None and not res.timed_out


def test_run_capture_times_out_and_stops_the_tree(tmp_path):
    """時間切れで止める（子の子まで。子の子が出力のパイプを持ったまま残ると communicate が返らず、
    left_running が立つ）。"""
    code = ("import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "print('started', flush=True); time.sleep(60)")
    res = run_capture([sys.executable, "-c", code], cwd=tmp_path, env=None, timeout=3)
    assert res.timed_out and not res.left_running and not res.ok and res.returncode is None
    assert "started" in res.output


class _HungProc:
    """communicate がいつまでも返らないプロセス。"""
    pid = 1

    def __init__(self):
        self.killed = 0

    def communicate(self, timeout=None):
        raise subprocess.TimeoutExpired("x", timeout)

    def kill(self):
        self.killed += 1


def test_stop_timed_out_reports_left_running(monkeypatch):
    killed_trees = []
    monkeypatch.setattr(P, "kill_tree", killed_trees.append)
    monkeypatch.setattr(P, "_REAP_SECONDS", 0)
    proc = _HungProc()
    res = P._stop_timed_out(proc)
    assert res.timed_out and res.left_running and not res.ok
    assert killed_trees == [proc] and proc.killed == 1


# ----- clip_output -----

def numbered(n):
    return "\n".join(f"line{i}" for i in range(1, n + 1))


def test_clip_output():
    assert clip_output(numbered(40)) == numbered(40)
    out = clip_output(numbered(100), max_lines=9, marker="--{n}--").splitlines()
    assert out == ["line1", "line2", "--92--", *[f"line{i}" for i in range(95, 101)]]
    assert clip_output(numbered(3), max_lines=2, marker="[{n}]").splitlines() == ["[2]", "line3"]
    with pytest.raises(ValueError):
        clip_output("", max_lines=1)


# ----- current_branch -----

ID_A = "a" * 40
ID_B = "0123456789ABCDEF" * 4     # 64 桁（大文字は小文字にして返す）


def make_git_dir(root, head):
    gitdir = root / ".git"
    (gitdir / "refs" / "heads").mkdir(parents=True)
    (gitdir / "HEAD").write_text(head + "\n", encoding="utf-8")
    return gitdir


def test_current_branch(tmp_path):
    make_git_dir(tmp_path / "a", "ref: refs/heads/main")
    make_git_dir(tmp_path / "b", "ref: refs/heads/feature/x")
    make_git_dir(tmp_path / "c", ID_A)
    make_git_dir(tmp_path / "d", "ref: refs/tags/v1")
    assert current_branch(tmp_path / "a") == "main"
    assert current_branch(tmp_path / "b") == "feature/x"
    assert current_branch(tmp_path / "c") is None            # detached
    assert current_branch(tmp_path / "d") is None
    assert current_branch(tmp_path / "none") is None


def test_current_branch_worktree(tmp_path):
    common = make_git_dir(tmp_path / "main", "ref: refs/heads/main")
    wt_git = common / "worktrees" / "wt"
    wt_git.mkdir(parents=True)
    (wt_git / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
    (wt_git / "commondir").write_text("../..\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {wt_git}\n", encoding="utf-8")
    assert current_branch(wt) == "feature"


# ----- read_head（pjdeck から）-----

def test_read_head_follows_a_loose_ref(tmp_path):
    gitdir = make_git_dir(tmp_path, "ref: refs/heads/main")
    (gitdir / "refs" / "heads" / "main").write_text(ID_A + "\n", encoding="utf-8")
    assert read_head(tmp_path) == ID_A
    assert read_head(str(tmp_path)) == ID_A


def test_read_head_reads_packed_refs(tmp_path):
    gitdir = make_git_dir(tmp_path, "ref: refs/heads/main")
    (gitdir / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        f"{'c' * 40} refs/heads/other\n"
        f"{ID_B} refs/heads/main\n"
        f"^{'d' * 40}\n", encoding="utf-8")
    assert read_head(tmp_path) == ID_B.lower()


def test_read_head_loose_ref_wins_over_packed(tmp_path):
    gitdir = make_git_dir(tmp_path, "ref: refs/heads/main")
    (gitdir / "packed-refs").write_text(f"{'c' * 40} refs/heads/main\n", encoding="utf-8")
    (gitdir / "refs" / "heads" / "main").write_text(ID_A, encoding="utf-8")
    assert read_head(tmp_path) == ID_A


def test_read_head_detached(tmp_path):
    make_git_dir(tmp_path, ID_B)
    assert read_head(tmp_path) == ID_B.lower()


def test_read_head_gitdir_file_relative(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    real = tmp_path / "store" / "repo.git"
    (real / "refs" / "heads").mkdir(parents=True)
    (real / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (real / "refs" / "heads" / "main").write_text(ID_A, encoding="utf-8")
    (repo / ".git").write_text("gitdir: ../store/repo.git\n", encoding="utf-8")
    assert read_head(repo) == ID_A


def test_read_head_worktree_reads_refs_from_commondir(tmp_path):
    main = tmp_path / "main"
    common = make_git_dir(main, "ref: refs/heads/main")
    (common / "refs" / "heads" / "feature").write_text(ID_B, encoding="utf-8")
    wt_git = common / "worktrees" / "wt"
    wt_git.mkdir(parents=True)
    (wt_git / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
    (wt_git / "commondir").write_text("../..\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {wt_git}\n", encoding="utf-8")
    assert read_head(wt) == ID_B.lower()


@pytest.mark.parametrize("setup", ["none", "empty_head", "bad_id", "missing_ref", "ref_to_ref",
                                   "bad_gitdir_file", "gitdir_missing"])
def test_read_head_none_when_unreadable(tmp_path, setup):
    if setup == "none":
        pass
    elif setup == "empty_head":
        make_git_dir(tmp_path, "")
    elif setup == "bad_id":
        make_git_dir(tmp_path, "abc123")
    elif setup == "missing_ref":
        make_git_dir(tmp_path, "ref: refs/heads/main")
    elif setup == "ref_to_ref":
        gitdir = make_git_dir(tmp_path, "ref: refs/heads/main")
        (gitdir / "refs" / "heads" / "main").write_text("ref: refs/heads/other", encoding="utf-8")
        (gitdir / "refs" / "heads" / "other").write_text(ID_A, encoding="utf-8")
    elif setup == "bad_gitdir_file":
        (tmp_path / ".git").write_text("nonsense\n", encoding="utf-8")
    elif setup == "gitdir_missing":
        (tmp_path / ".git").write_text("gitdir: nowhere\n", encoding="utf-8")
    assert read_head(tmp_path) is None


# ----- read_commit_time（オブジェクトのファイルを読む。git は呼ばない）-----

COMMIT_TIME = 1792090838     # UTC では 2026-10-15 19:00:38、+0900 では 2026-10-16 04:00:38
JST = timezone(timedelta(hours=9))


def commit_body(when=COMMIT_TIME, tz="+0900", extra=b"", message=b"message\n"):
    """コミットの中身（author の時刻は committer と違えて、committer の方を読むことを確かめる）。"""
    return (b"tree " + b"4" * 40 + b"\n"
            b"author A <a@example.com> 1 +0000\n"
            + f"committer C <c@example.com> {when} {tz}\n".encode() + extra + b"\n" + message)


def raw_object(kind: bytes, data: bytes) -> bytes:
    return kind + b" " + str(len(data)).encode() + b"\0" + data


def object_id(kind: bytes, data: bytes, algo=hashlib.sha1) -> str:
    return algo(raw_object(kind, data)).hexdigest()


def put_loose(objects, kind, data, *, raw=None, algo=hashlib.sha1):
    """loose を置いて id を返す。raw を渡せば、展開した中身をそれにする。"""
    oid = object_id(kind, data, algo)
    path = objects / oid[:2] / oid[2:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(zlib.compress(raw_object(kind, data) if raw is None else raw))
    return oid


def git_objects(root):
    return make_git_dir(root, "ref: refs/heads/main") / "objects"


def ymd(when):
    return when and (when.year, when.month, when.day, when.utcoffset())


def pack_header(kind, size):
    out = bytearray()
    c = (kind << 4) | (size & 0x0F)
    size >>= 4
    while size:
        out.append(c | 0x80)
        c = size & 0x7F
        size >>= 7
    out.append(c)
    return bytes(out)


def ofs_bytes(rel):
    """OFS_DELTA の基までの距離（git の pack-objects と同じ符号化）。"""
    out = [rel & 0x7F]
    rel >>= 7
    while rel:
        rel -= 1
        out.append(0x80 | (rel & 0x7F))
        rel >>= 7
    return bytes(reversed(out))


def varint(n):
    out = bytearray()
    while n >= 0x80:
        out.append(0x80 | (n & 0x7F))
        n >>= 7
    out.append(n)
    return bytes(out)


def copy_op(start, size):
    op, args = 0x80, bytearray()
    for i in range(4):
        if (start >> (8 * i)) & 0xFF:
            op |= 1 << i
            args.append((start >> (8 * i)) & 0xFF)
    for i in range(3):
        if (size >> (8 * i)) & 0xFF:
            op |= 0x10 << i
            args.append((size >> (8 * i)) & 0xFF)
    return bytes([op]) + bytes(args)


def make_delta(base, target):
    """base → target の delta（共通の前を copy、違うところを insert、共通の後を位置付きの copy）。"""
    p = 0
    while p < min(len(base), len(target)) and base[p] == target[p]:
        p += 1
    s = 0
    while s < min(len(base), len(target)) - p and base[-1 - s] == target[-1 - s]:
        s += 1
    middle = target[p:len(target) - s]
    assert p and s and 0 < len(middle) < 0x80
    return (varint(len(base)) + varint(len(target)) + copy_op(0, p)
            + bytes([len(middle)]) + middle + copy_op(len(base) - s, s))


class Pack:
    """手で組む pack と idx v2（チェックサムと CRC は 0 で埋める。read_commit_time は確かめない）。"""

    def __init__(self):
        self.data = bytearray(b"PACK" + struct.pack(">II", 2, 0))
        self.offsets = {}

    def add(self, oid, kind, payload, base=b""):
        """1 件を足す。base は OFS なら ofs_bytes、REF なら基の id のバイト列。"""
        self.offsets[oid] = len(self.data)
        self.data += pack_header(kind, len(payload)) + base + zlib.compress(payload)

    def add_ofs(self, oid, base_oid, delta):
        self.add(oid, 6, delta, ofs_bytes(len(self.data) - self.offsets[base_oid]))

    def write(self, objects, name, *, version=2, large=False):
        """pack-<name>.pack / .idx を書く。large なら位置を全部 8 バイトの表に回す。"""
        folder = objects / "pack"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"pack-{name}.pack").write_bytes(bytes(self.data) + bytes(20))
        names = sorted(bytes.fromhex(o) for o in self.offsets)
        idx = bytearray(b"\xfftOc" + struct.pack(">I", version))
        idx += struct.pack(">256I", *(sum(1 for n in names if n[0] <= i) for i in range(256)))
        idx += b"".join(names) + bytes(4 * len(names))
        offsets = [self.offsets[n.hex()] for n in names]
        if large:
            idx += b"".join(struct.pack(">I", 0x80000000 | i) for i in range(len(offsets)))
            idx += b"".join(struct.pack(">Q", o) for o in offsets)
        else:
            idx += b"".join(struct.pack(">I", o) for o in offsets)
        (folder / f"pack-{name}.idx").write_bytes(bytes(idx) + bytes(40))


def test_read_commit_time_loose_uses_recorded_offset(tmp_path):
    oid = put_loose(git_objects(tmp_path), b"commit", commit_body())
    got = read_commit_time(tmp_path, oid)
    assert got == datetime.fromtimestamp(COMMIT_TIME, UTC)
    assert got.utcoffset() == timedelta(hours=9)
    assert got.date() == date(2026, 10, 16)                          # 記録された +0900 では 16 日
    assert got.astimezone(UTC).date() == date(2026, 10, 15)  # UTC では 15 日
    assert read_commit_time(str(tmp_path), oid.upper()) == got


def test_read_commit_time_negative_offset_and_signature(tmp_path):
    sig = (b"gpgsig -----BEGIN PGP SIGNATURE-----\n"
           b" \n"
           b" iQEzBAABCAAdFiEE\n"
           b" -----END PGP SIGNATURE-----\n")
    oid = put_loose(git_objects(tmp_path), b"commit", commit_body(tz="-0730", extra=sig))
    got = read_commit_time(tmp_path, oid)
    assert got.utcoffset() == -timedelta(hours=7, minutes=30)
    assert (got.year, got.month, got.day, got.hour, got.minute) == (2026, 10, 15, 11, 30)


def test_read_commit_time_reads_objects_of_commondir(tmp_path):
    main = tmp_path / "main"
    common = make_git_dir(main, "ref: refs/heads/main")
    oid = put_loose(common / "objects", b"commit", commit_body())
    wt_git = common / "worktrees" / "wt"
    wt_git.mkdir(parents=True)
    (wt_git / "HEAD").write_text(oid + "\n", encoding="utf-8")
    (wt_git / "objects").mkdir()                 # worktree の gitdir の objects は見ない
    (wt_git / "commondir").write_text("../..\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {wt_git}\n", encoding="utf-8")
    assert ymd(read_commit_time(wt, oid)) == (2026, 10, 16, timedelta(hours=9))

    repo = tmp_path / "repo"                     # .git がファイル（相対の gitdir:）
    repo.mkdir()
    (repo / ".git").write_text("gitdir: ../main/.git\n", encoding="utf-8")
    assert ymd(read_commit_time(repo, oid)) == (2026, 10, 16, timedelta(hours=9))


@pytest.mark.parametrize("large", [False, True])
def test_read_commit_time_from_pack_with_deltas(tmp_path, large):
    objects = git_objects(tmp_path)
    bodies = {
        "base": commit_body(when=1000000000, tz="+0000"),          # 2001-09-09 01:46:40 UTC
        "ofs1": commit_body(),                                     # base からの OFS
        "ofs2": commit_body(when=COMMIT_TIME + 86400),             # ofs1 からの OFS（2 段）
        "ref": commit_body(when=COMMIT_TIME - 86400, tz="-0700"),  # base からの REF
    }
    ids = {k: object_id(b"commit", v) for k, v in bodies.items()}
    pack = Pack()
    pack.add(ids["base"], 1, bodies["base"])
    pack.add_ofs(ids["ofs1"], ids["base"], make_delta(bodies["base"], bodies["ofs1"]))
    pack.add_ofs(ids["ofs2"], ids["ofs1"], make_delta(bodies["ofs1"], bodies["ofs2"]))
    pack.add(ids["ref"], 7, make_delta(bodies["base"], bodies["ref"]), bytes.fromhex(ids["base"]))
    blob = object_id(b"blob", b"x")
    pack.add(blob, 3, b"x")
    pack.write(objects, "1", large=large)

    assert ymd(read_commit_time(tmp_path, ids["base"])) == (2001, 9, 9, timedelta(0))
    assert ymd(read_commit_time(tmp_path, ids["ofs1"])) == (2026, 10, 16, timedelta(hours=9))
    assert ymd(read_commit_time(tmp_path, ids["ofs2"])) == (2026, 10, 17, timedelta(hours=9))
    assert ymd(read_commit_time(tmp_path, ids["ref"])) == (2026, 10, 14, -timedelta(hours=7))
    assert read_commit_time(tmp_path, blob) is None
    assert read_commit_time(tmp_path, object_id(b"commit", b"other")) is None   # どの pack にも無い


def test_read_commit_time_ref_delta_to_loose_base(tmp_path):
    objects = git_objects(tmp_path)
    base, target = commit_body(when=1000000000), commit_body()
    put_loose(objects, b"commit", base)
    oid = object_id(b"commit", target)
    pack = Pack()
    pack.add(oid, 7, make_delta(base, target), bytes.fromhex(object_id(b"commit", base)))
    pack.write(objects, "1")
    assert ymd(read_commit_time(tmp_path, oid)) == (2026, 10, 16, timedelta(hours=9))


def test_read_commit_time_sha256(tmp_path):
    objects = git_objects(tmp_path)
    base, target = commit_body(when=1000000000), commit_body()
    loose = put_loose(objects, b"commit", base, algo=hashlib.sha256)
    oid = object_id(b"commit", target, hashlib.sha256)
    pack = Pack()
    pack.add(oid, 7, make_delta(base, target), bytes.fromhex(loose))   # 基の id は 32 バイト
    pack.write(objects, "1")
    assert len(oid) == 64
    assert ymd(read_commit_time(tmp_path, loose)) == (2001, 9, 9, timedelta(hours=9))
    assert ymd(read_commit_time(tmp_path, oid)) == (2026, 10, 16, timedelta(hours=9))


def test_read_commit_time_skips_unreadable_packs(tmp_path):
    objects = git_objects(tmp_path)
    body = commit_body()
    oid = object_id(b"commit", body)
    for name in ("0truncated", "0bad_zlib", "0no_pack", "0v3"):
        pack = Pack()
        pack.add(oid, 1, body)
        pack.write(objects, name, version=3 if name == "0v3" else 2)
        path = objects / "pack" / f"pack-{name}.pack"
        if name == "0truncated":
            path.write_bytes(path.read_bytes()[:13])        # 見出しの途中で切れている
        elif name == "0bad_zlib":
            start = 12 + len(pack_header(1, len(body)))
            path.write_bytes(path.read_bytes()[:start] + b"\xff" * 40)
        elif name == "0no_pack":
            path.unlink()                                   # gc の最中に消えた
    assert read_commit_time(tmp_path, oid) is None
    good = Pack()
    good.add(oid, 1, body)
    good.write(objects, "1good")                            # 名前の順で壊れたものの後ろ
    assert ymd(read_commit_time(tmp_path, oid)) == (2026, 10, 16, timedelta(hours=9))


@needs_git
def test_read_commit_time_real_repo_loose_and_packed(isolated, monkeypatch):
    run(isolated, "init", "-q", "r")
    repo = isolated / "r"
    message = "version\n\n" + "the same body line\n" * 200      # 似たコミットは delta になりうる
    dates = ["1792090838 +0900", "1792000000 -0700", "1700000000 +0530", "1792090838 +0000",
             "1600000000 -1200"]
    for i, when in enumerate(dates):
        (repo / "f.txt").write_text(f"{i}\n")
        run(repo, "add", "f.txt")
        monkeypatch.setenv("GIT_COMMITTER_DATE", when)
        run(repo, "commit", "-q", "-m", f"{message}{i}")
    log = subprocess.run(["git", "log", "--format=%H %cd", "--date=format:%Y.%m.%d %H:%M:%S %z"],
                         cwd=repo, check=True, capture_output=True, text=True).stdout
    rows = [line.split(" ", 1) for line in log.splitlines()]
    assert len(rows) == len(dates)

    def ours(oid):
        t = read_commit_time(repo, oid)
        return f"{t.year}.{t.month:02d}.{t.day:02d} {t:%H:%M:%S %z}"

    head = read_head(repo)
    assert head == rows[0][0]
    loose = repo / ".git" / "objects" / head[:2] / head[2:]
    assert loose.exists()
    assert [ours(oid) for oid, _ in rows] == [want for _, want in rows]
    assert rows[-1][1] == "2026.10.16 04:00:38 +0900"             # 最初のコミット（log は新しい順）

    run(repo, "gc", "-q")
    assert not loose.exists()                                     # pack から読む
    idx = list((repo / ".git" / "objects" / "pack").glob("pack-*.idx"))
    assert len(idx) == 1
    packed = subprocess.run(["git", "verify-pack", "-v", str(idx[0])],
                            cwd=repo, check=True, capture_output=True, text=True).stdout
    # 本物の delta も通す（git 2.53 は似たコミットを基のコミットからの delta にする。列が 7 つなら delta）
    assert any(len(line.split()) == 7 for line in packed.splitlines() if " commit " in line), packed
    assert [ours(oid) for oid, _ in rows] == [want for _, want in rows]


@pytest.mark.parametrize("case", ["none", "bad_id", "short_id", "no_git", "missing", "bad_zlib", "blob",
                                  "size_mismatch", "no_committer", "tz_hours", "tz_minutes", "tz_no_sign",
                                  "bad_seconds", "idx_v3"])
def test_read_commit_time_none_when_unreadable(tmp_path, case):
    body = commit_body()
    oid = object_id(b"commit", body)
    objects = None if case == "no_git" else git_objects(tmp_path)
    if case == "none":
        oid = None
    elif case == "bad_id":
        oid = "../" + "a" * 37                          # 40 字でも 16 進数でなければ objects の外を作らない
    elif case == "short_id":
        put_loose(objects, b"commit", body)
        oid = oid[:7]
    elif case == "bad_zlib":
        path = objects / oid[:2] / oid[2:]
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not zlib")
    elif case == "blob":
        oid = put_loose(objects, b"blob", body)
    elif case == "size_mismatch":
        put_loose(objects, b"commit", body, raw=b"commit 5\0" + body)
    elif case == "no_committer":                        # 本文（空行の後）の committer は読まない
        oid = put_loose(objects, b"commit", b"tree x\nauthor A <a> 1 +0000\n\ncommitter C <c> 1 +0000\n")
    elif case in ("tz_hours", "tz_minutes", "tz_no_sign", "bad_seconds"):
        when, tz = {"tz_hours": (COMMIT_TIME, "+9999"), "tz_minutes": (COMMIT_TIME, "+0960"),
                    "tz_no_sign": (COMMIT_TIME, "0900"), "bad_seconds": ("17x", "+0900")}[case]
        oid = put_loose(objects, b"commit", commit_body(when=when, tz=tz))
    elif case == "idx_v3":
        pack = Pack()
        pack.add(oid, 1, body)
        pack.write(objects, "1", version=3)
    assert read_commit_time(tmp_path, oid) is None


def test_read_commit_time_delta_depth_limit(tmp_path, monkeypatch):
    objects = git_objects(tmp_path)
    bodies = [commit_body(when=COMMIT_TIME + i) for i in range(4)]
    ids = [object_id(b"commit", b) for b in bodies]
    pack = Pack()
    pack.add(ids[0], 1, bodies[0])
    for i in range(1, 4):
        pack.add_ofs(ids[i], ids[i - 1], make_delta(bodies[i - 1], bodies[i]))
    pack.write(objects, "1")
    monkeypatch.setattr(G, "_MAX_DELTA_DEPTH", 2)
    assert read_commit_time(tmp_path, ids[2]) is not None    # 2 段までは辿る
    assert read_commit_time(tmp_path, ids[3]) is None


def test_read_commit_time_ref_delta_cycle_stops(tmp_path):
    """自分を基にした REF_DELTA（壊れた pack）でも、段数の上限で止まる。"""
    objects = git_objects(tmp_path)
    body = commit_body()
    oid = object_id(b"commit", body)
    pack = Pack()
    pack.add(oid, 7, make_delta(body, commit_body(when=COMMIT_TIME + 1)), bytes.fromhex(oid))
    pack.write(objects, "1")
    assert read_commit_time(tmp_path, oid) is None


def test_read_commit_time_size_limits(tmp_path, monkeypatch):
    objects = git_objects(tmp_path)
    small = commit_body()
    grown = commit_body(message=b"y" * 100 + b"message\n")    # small より 100 バイト大きい
    big = commit_body(message=b"z" * 500)
    monkeypatch.setattr(G, "_MAX_OBJECT", len(small) + 10)
    small_id = put_loose(objects, b"commit", small)
    big_loose = put_loose(objects, b"commit", big)
    big_packed = object_id(b"commit", commit_body(message=b"w" * 500))
    grown_id = object_id(b"commit", grown)
    pack = Pack()
    pack.add(big_packed, 1, commit_body(message=b"w" * 500))
    pack.add(grown_id, 7, make_delta(small, grown), bytes.fromhex(small_id))
    pack.write(objects, "1")
    assert read_commit_time(tmp_path, small_id) is not None
    assert read_commit_time(tmp_path, big_loose) is None
    assert read_commit_time(tmp_path, big_packed) is None
    assert read_commit_time(tmp_path, grown_id) is None       # 組み立てた後が上限を超える
