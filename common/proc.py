"""Cross-platform subprocess helpers."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


def no_window_kwargs() -> dict:
    """Popen/run に渡すと Windows でコンソール窓を出さない kwargs を返す。

    windowless GUI（run.bat → pythonw、コンソール無し）から claude/pi エンジンや
    cloudflared・taskkill 等のコンソール子プロセスを spawn すると、抑止しない限り
    新しいコンソール窓が開く。POSIX では {} を返す（no-op）。

    ``subprocess.CREATE_NO_WINDOW`` は win32 の Python にのみ存在する。テストが
    ``sys.platform`` をグローバルに "win32" へ monkeypatch した POSIX 環境でも
    この分岐に入り得るため、直接参照ではなく ``getattr(..., 0)`` で取得する
    （0 は Popen の既定 creationflags なので害がない）。
    """
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


# npm シム内の "%dp0%\..." / "%~dp0\..." 参照（cmd-shim が生成する 2 形式）。
_SHIM_TOKEN_RE = re.compile(r'"%(?:~)?dp0%?\\+([^"]+)"')


def resolve_cmd_shim(bin_path: str) -> list[str]:
    """npm の .cmd/.bat シムを実体の起動 argv へ解決する。失敗は []。

    cmd.exe /c ラップは**引数中の最初の改行で残り全部を切断する**（実測:
    5603 文字の system プロンプトが 372 文字になり、後続の --resume ごと消えた）。
    複数行引数を渡すバックエンド spawn はシムを cmd.exe 経由で起動してはならず、
    シムが指す実体を直接 spawn する。npm シムの中身は安定して 2 形式:

    - 実行ファイル直接型（claude: "%dp0%\\node_modules\\...\\claude.exe" %*）
      → [exe]
    - node + JS エントリ型（pi/codex: node "%dp0%\\node_modules\\...\\cli.js" %*）
      → [node, entry_js]（node はシム隣接 node.exe → PATH の順）

    解決できないシム（自作 .bat 等）は [] を返し、呼出側が従来の cmd.exe ラップへ
    フォールバックする（単一行引数なら従来どおり動く）。
    """
    try:
        text = Path(bin_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    shim_dir = Path(bin_path).parent
    targets: list[Path] = []
    for rel in _SHIM_TOKEN_RE.findall(text):
        cand = shim_dir / rel.replace("\\", os.sep).lstrip(os.sep)
        if cand.is_file() and cand not in targets:
            targets.append(cand)
    for p in targets:
        if p.suffix.lower() == ".exe" and p.name.lower() != "node.exe":
            return [str(p)]
    for p in targets:
        if p.suffix.lower() in (".js", ".cjs", ".mjs"):
            node = next(
                (str(t) for t in targets if t.name.lower() == "node.exe"), None
            ) or shutil.which("node")
            return [node, str(p)] if node else []
    return []


_TASKKILL_TIMEOUT_S = 10


def kill_tree(proc: subprocess.Popen) -> None:
    """プロセスをツリーごと強制終了する。never raise。

    Windows は ``taskkill /F /T``（返らなくても _TASKKILL_TIMEOUT_S で切る）。POSIX は
    プロセスグループに SIGKILL を送るので、``start_new_session=True`` で起動したもの
    （pid = PGID）にだけ使う。
    """
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=_TASKKILL_TIMEOUT_S,
                **no_window_kwargs(),
            )
        else:
            import signal

            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001, S110 — 既に終了済み・taskkill の時間切れも含め、呼び出し側が終わりを待って確かめる
        pass


_REAP_SECONDS = 5   # 時間切れの後、kill_tree → 終わりを待つ（2 段目は kill() → 待つ）


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    output: str                     # stdout と stderr をまとめたもの（UTF-8、読めないバイトは置き換え）
    returncode: int | None = None   # 終わらなかった・起動できなかったときは None
    timed_out: bool = False
    left_running: bool = False      # 時間切れで止めようとしたが、止まったか確かめられなかった


def run_capture(argv: list[str], *, cwd, env: dict[str, str] | None,
                timeout: float) -> CommandResult:
    """argv を実行して出力をまとめて返す。never raise。

    stdin は DEVNULL、コンソール窓は出さない（no_window_kwargs）。POSIX では新しいセッションで
    起動し、時間切れはツリーごと止める（kill_tree → 待つ、まだなら kill() → 待つ）。
    """
    try:
        proc = subprocess.Popen(
            argv, cwd=None if cwd is None else str(cwd), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=(sys.platform != "win32"), **no_window_kwargs(),
        )
    except Exception as e:  # noqa: BLE001 — OSError のほか、引数の型違いも結果で返す
        return CommandResult(False, f"could not start {argv[0] if argv else '?'}: {e}")
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        return _stop_timed_out(proc)
    except Exception as e:  # noqa: BLE001
        kill_tree(proc)
        return CommandResult(False, f"{type(e).__name__}: {e}")
    return CommandResult(proc.returncode == 0, _decode(out), proc.returncode)


def _decode(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace")


def _stop_timed_out(proc: subprocess.Popen) -> CommandResult:
    """時間切れのプロセスを止める。communicate が返れば（本体も、出力のパイプを受け継いだ子も
    終わった）止まったとする。"""
    kill_tree(proc)
    stopped, out = _reap(proc)
    if not stopped:
        try:
            proc.kill()
        except OSError:
            pass
        stopped, out = _reap(proc)
    return CommandResult(False, _decode(out), None, timed_out=True, left_running=not stopped)


def _reap(proc: subprocess.Popen) -> tuple[bool, bytes]:
    try:
        out, _ = proc.communicate(timeout=_REAP_SECONDS)
    except subprocess.TimeoutExpired:
        return False, b""
    return True, out or b""
