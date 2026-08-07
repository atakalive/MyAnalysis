"""対応 LLM ドライバの導入状況を棚卸しする（Qt 非依存・never raise）。

「エンジンが入っていない／認証されていない」ことに送信して初めて気づく、という状態を
潰すための検出層。各バックエンドの検出は ``stream()`` の中にしか無かったので、そこを
呼ばずに状態だけ知る手段をここに集める。

**これは「今どのエンジンを選んでいるか」とは無関係な一覧**である。全ドライバを同じ形で並べ、
claude は「VS Code 同梱」と「PATH の CLI」を別々の入手経路として独立に見る。

段階は **0. 前提(node+npm) → 1. 導入 → 2. 認証**。0 を 1 に混ぜると「インストールを押したのに
失敗する」になるので分けてある。

課金しない: ここから LLM 推論は一切走らない（実際に 1 ターン投げる疎通確認は
``llm_backend.ping`` 側で、ユーザーが明示的に押したときだけ動く）。

**``pi auth print-bearer-token`` を呼んではならない。** 既定で「30 分以内に切れるなら
リフレッシュ」する仕様で、同じ refresh token を pi(WSL) / pi(Windows) / codex CLI で
共有している環境では、状態を見ただけで他がログアウトし得る。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from common.proc import no_window_kwargs

# --version は速い（実測 107-2623ms）。--list-models はネットワークに出る（実測 4657ms）。
# 余裕を持たせつつ、固まったバイナリでワーカーを塞がない値。
_VERSION_TIMEOUT = 15.0
_PROBE_TIMEOUT = 30.0

# pi の package.json の engines.node。ここを満たさないと npm i が通らない。
_PI_MIN_NODE = (22, 19)

_NPM = "npm"


@dataclass(frozen=True)
class EngineStatus:
    """1 ドライバ分の棚卸し結果。

    state 系は ``"ok" | "missing" | "unknown" | "n/a"``。**``unknown`` と ``missing`` を
    混ぜないこと**: タイムアウトや実行失敗を ``missing`` にすると「遅いだけ／オフラインなだけ」
    で「入っていません」と表示し、既に入っているものの再インストールを勧めてしまう。
    """

    engine_id: str
    prereq_state: str = "n/a"       # 0 段目: node + npm
    prereq_detail: str = ""
    binary_state: str = "n/a"       # 1 段目: 導入
    binary: str | None = None
    version: str | None = None
    auth_state: str = "n/a"         # 2 段目: 認証
    auth_detail: str = ""
    authed_providers: tuple[str, ...] = ()
    install: tuple[str, ...] | None = None   # npm コマンド（不可/不要なら None）
    login: tuple[str, ...] | None = None     # 端末で起こすコマンド（不可/不要なら None）
    # 人間向けの文は **ここで作らない**。このモジュールは Qt だけでなく i18n も知らない
    # （CLI と GUI の両方から使うので、日本語を埋め込むと en 表示に混ざる）。
    # detail 系はバージョン番号・provider 名・外部ツールの生出力など **中立な事実**だけを入れ、
    # 説明文は (i18n キー, params) を notes に入れて呼び出し側に訳させる。
    notes: tuple[tuple[str, dict], ...] = ()
    note_key: str | None = None              # i18n キー（VS Code 拡張を入れて 等）

    @property
    def blocking_stage(self) -> str | None:
        """最初に詰まっている段。``None`` なら通っている。UI はここだけ強調する。"""
        if self.prereq_state == "missing":
            return "prereq"
        if self.binary_state == "missing":
            return "binary"
        if self.auth_state == "missing":
            return "auth"
        return None


# ----- 低レベル -----


def _wrap(cmd: list[str]) -> list[str]:
    """実際に起動できる argv へ直す（PATH 解決 → win32 のシム包み）。

    2 段階どちらも必要:

    1. **裸の名前を解決する。** win32 で ``npm`` は実行ファイルではなく ``npm.cmd`` で、
       ``Popen(["npm", ...])`` は WinError 2 になる。``pi`` / ``claude`` も同じ。
    2. **``.cmd``/``.bat`` は ``cmd.exe /c`` で包む。** CreateProcess はシムを直接
       起動できない（pi.py:104 / codex.py:133 と同じ既知の罠）。

    1 を忘れると「インストール」「ログイン」ボタンが Windows で必ず失敗する。
    """
    if not cmd:
        return cmd
    exe = _which(cmd[0]) or cmd[0]
    out = [exe] + list(cmd[1:])
    if sys.platform == "win32" and exe.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c"] + out
    return out


def _run(cmd: list[str], timeout: float) -> tuple[int, str] | None:
    """コマンドを実行し ``(returncode, stdout+stderr)`` を返す。never raise。

    ``None`` は「判定できなかった」= タイムアウト / 起動失敗。呼び出し側はこれを
    ``missing`` ではなく ``unknown`` に落とすこと。

    encoding/errors は cp932 コンソールで壊れないように明示する（tunnel.py と同じ理由）。
    """
    try:
        p = subprocess.run(
            _wrap(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            text=True,
            encoding="utf-8",
            errors="replace",
            **no_window_kwargs(),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        # FileNotFoundError（実体なし）も TimeoutExpired もここ。区別せず「不明」。
        return None
    return p.returncode, (p.stdout or "")


_VER_RE = re.compile(r"\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.-]+)?")


def _parse_version(text: str | None) -> str | None:
    """出力からバージョンらしき文字列を拾う。

    形はエンジンごとに違う（実測: pi は ``0.83.0``、codex は ``codex-cli 0.146.0``、
    node は ``v24.18.0``）。数字の並びを拾い、拾えなければ 1 行目をそのまま返す
    — 表示目的なので厳密でなくてよい。
    """
    if not text:
        return None
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    if not first:
        return None
    m = _VER_RE.search(first)
    return m.group(0) if m else first[:60]


def _which(name: str) -> str | None:
    """PATH 解決。win32 では npm の実行不能な拡張子なし shim を避ける。"""
    if sys.platform == "win32":
        for cand in (name + ".exe", name + ".cmd", name + ".bat"):
            found = shutil.which(cand)
            if found:
                return found
    return shutil.which(name)


def _version_tuple(v: str | None) -> tuple[int, ...]:
    if not v:
        return ()
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


# ----- 0 段目: node + npm -----


@dataclass
class _Toolchain:
    node: str | None = None
    npm: str | None = None
    node_missing: bool = False
    npm_missing: bool = False


def _toolchain() -> _Toolchain:
    """node と npm を **別々に** 見る。

    インストールで実際に叩くのは ``npm`` であって ``node`` ではない。実測でも npm は
    node 本体とは別実体（`C:\\Program Files\\nodejs\\npm.cmd`）で、nvm 構成などでは
    node はあるが npm が無いことが起こり得る。
    """
    tc = _Toolchain()
    if _which("node") is None:
        tc.node_missing = True
    else:
        r = _run(["node", "--version"], _VERSION_TIMEOUT)
        tc.node = _parse_version(r[1]) if r and r[0] == 0 else None
    npm_path = _which(_NPM)
    if npm_path is None:
        tc.npm_missing = True
    else:
        r = _run([npm_path, "--version"], _VERSION_TIMEOUT)
        tc.npm = _parse_version(r[1]) if r and r[0] == 0 else None
    return tc


def _prereq(tc: _Toolchain, min_node):
    """``(state, detail, notes)``。detail は中立な事実だけ、理由は notes の i18n キーで返す。"""
    if tc.node_missing or tc.npm_missing:
        miss = " / ".join(
            n for n, m in (("node", tc.node_missing), ("npm", tc.npm_missing)) if m
        )
        return "missing", "", (("backend.status.note.missing_tools", {"tools": miss}),)
    detail = f"node {tc.node or '?'} / npm {tc.npm or '?'}"
    if min_node and tc.node:
        cur = _version_tuple(tc.node)
        if cur and cur < min_node:
            need = ".".join(str(x) for x in min_node)
            return "missing", detail, (
                ("backend.status.note.node_too_old", {"need": need}),
            )
    return "ok", detail, ()


# ----- 2 段目: 認証 -----


def _claude_auth() -> tuple[str, str]:
    """``~/.claude/.credentials.json`` の有無。

    env の API キー経路もあるので**推定でしかない**。断定的な ✗ ではなく「未検出」に留める。
    """
    p = Path.home() / ".claude" / ".credentials.json"
    try:
        if p.is_file() and p.stat().st_size > 0:
            return "ok", ""
    except OSError:
        return "unknown", ""
    return "missing", ""


def _pi_providers_from_list_models(pi_bin: str) -> tuple[str, ...] | None:
    """``pi --list-models`` の provider 列を拾う。判定できなければ None。

    ここが最も正確な認証判定になる: 返るのは**実際に使える** provider だけで、
    auth.json にトークンが残っていても失効していれば出てこない（実測で
    google-gemini-cli がそうだった）。
    """
    r = _run([pi_bin, "--list-models"], _PROBE_TIMEOUT)
    if r is None or r[0] != 0:
        return None
    out: list[str] = []
    for line in r[1].splitlines():
        tok = line.split()
        if not tok:
            continue
        name = tok[0]
        if name == "provider":          # ヘッダ行
            continue
        if name not in out:
            out.append(name)
    return tuple(out) if out else None


def _pi_providers_from_authfile() -> tuple[str, ...] | None:
    """オフライン時のフォールバック。トークンの**有無**しか分からない（失効は見抜けない）。"""
    p = Path.home() / ".pi" / "agent" / "auth.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return tuple(k for k in data if isinstance(k, str))


def _codex_auth(codex_bin: str) -> tuple[str, str]:
    """``codex login status``（公式の非対話サブコマンド。auth.json を書き換えない）。"""
    r = _run([codex_bin, "login", "status"], _VERSION_TIMEOUT)
    if r is None:
        return "unknown", ""
    text = r[1].strip().splitlines()
    detail = text[0].strip() if text else ""
    return ("ok", detail) if r[0] == 0 else ("missing", detail)


def _openai_env_detail() -> tuple[str, str]:
    """openai-http は**合否にしない**。

    ``_make_openai`` は ``OPENAI_API_KEY`` が無ければ ``"not-needed"`` を使い、
    ``OPENAI_BASE_URL`` が無ければ本家を向く。ローカル Ollama 等ではキー無しが正常なので、
    env の設定有無を事実として出すだけにする。
    """
    have = [
        n for n in ("OPENAI_BASE_URL", "OPENAI_API_KEY") if os.environ.get(n)
    ]
    return "n/a", ", ".join(have)


# ----- 1 段目: バイナリ解決（既存の探索を再利用する） -----


def _resolvable(path: str | None) -> bool:
    """その文字列で本当に起動できるか。

    ``ClaudeCodeBackend._resolve_bin`` は PATH に無くても **裸の名前をそのまま返す**
    （`shutil.which(value) or value`）。そのまま ok 扱いすると「入っていないのに
    unknown」になり、導入ボタンまで消えてしまう。ここで実体を確かめて missing に落とす。
    """
    if not path:
        return False
    return os.path.isfile(path) or _which(path) is not None


def _claude_binary(bin_value: str) -> tuple[str, str | None]:
    """claude の入手経路を **固定の bin で** 探す。

    現在の設定は渡さない — 一覧は「今どれを選んでいるか」と無関係だから。
    ``""`` なら VS Code 拡張を、``"claude"`` なら PATH を見させる。
    """
    try:
        from llm_backend.claude_code import ClaudeCodeBackend
        path = ClaudeCodeBackend({"bin": bin_value})._discover_binary()
    except Exception:
        return "missing", None
    return ("ok", path) if _resolvable(path) else ("missing", None)


def _codex_binary() -> tuple[str, str | None]:
    try:
        from llm_backend.codex import CodexBackend
        return "ok", CodexBackend({})._discover_binary()
    except Exception:
        return "missing", None


# ----- 公開 API -----


def check_engine(engine_id: str) -> EngineStatus:
    """1 ドライバ分の状態を返す。**never raise**（どんな失敗も state に畳む）。"""
    try:
        return _check_engine(engine_id)
    except Exception:                       # pragma: no cover - 保険
        return EngineStatus(engine_id=engine_id, binary_state="unknown")


def _check_engine(engine_id: str) -> EngineStatus:
    tc = _toolchain()

    if engine_id in ("claude-vscode", "claude-cli"):
        vscode = engine_id == "claude-vscode"
        state, path = _claude_binary("" if vscode else "claude")
        ver = None
        if state == "ok" and path:
            r = _run([path, "--version"], _VERSION_TIMEOUT)
            if r is None:
                state = "unknown"           # 起動できない/固まった → 「無い」ではない
            elif r[0] == 0:
                ver = _parse_version(r[1])
        auth_state, auth_detail = _claude_auth()
        if vscode:
            return EngineStatus(
                engine_id=engine_id, binary_state=state, binary=path, version=ver,
                auth_state=auth_state, auth_detail=auth_detail,
                note_key="backend.status.note.vscode_ext",
            )
        pre_state, pre_detail, notes = _prereq(tc, None)
        return EngineStatus(
            engine_id=engine_id, prereq_state=pre_state, prereq_detail=pre_detail,
            binary_state=state, binary=path, version=ver,
            auth_state=auth_state, auth_detail=auth_detail, notes=notes,
            install=(_NPM, "i", "-g", "@anthropic-ai/claude-code")
            if state == "missing" and pre_state == "ok" else None,
            login=("claude",) if state == "ok" else None,
        )

    if engine_id == "pi":
        pre_state, pre_detail, notes = _prereq(tc, _PI_MIN_NODE)
        path = _which("pi")
        state = "ok" if path else "missing"
        ver = None
        providers: tuple[str, ...] = ()
        auth_state, auth_detail = "unknown", ""
        if path:
            r = _run([path, "--version"], _VERSION_TIMEOUT)
            if r is None:
                state = "unknown"
            elif r[0] == 0:
                ver = _parse_version(r[1])
            live = _pi_providers_from_list_models(path)
            if live is not None:
                providers, auth_state = live, "ok"
            else:
                cached = _pi_providers_from_authfile()
                if cached:
                    providers, auth_state = cached, "unknown"
                    notes = notes + (("backend.status.note.offline_auth", {}),)
                else:
                    auth_state = "missing"
        return EngineStatus(
            engine_id=engine_id, prereq_state=pre_state, prereq_detail=pre_detail,
            binary_state=state, binary=path, version=ver,
            auth_state=auth_state, auth_detail=auth_detail, authed_providers=providers,
            notes=notes,
            install=(_NPM, "i", "-g", "@earendil-works/pi-coding-agent")
            if state == "missing" and pre_state == "ok" else None,
            # pi の /login は対話 TUI。端末を起こしてユーザーに操作してもらうしかない。
            login=("pi",) if state == "ok" else None,
        )

    if engine_id == "codex":
        pre_state, pre_detail, notes = _prereq(tc, None)
        state, path = _codex_binary()
        ver = None
        auth_state, auth_detail = "unknown", ""
        if state == "ok" and path:
            r = _run([path, "--version"], _VERSION_TIMEOUT)
            if r is None:
                state = "unknown"
            elif r[0] == 0:
                ver = _parse_version(r[1])
            if state == "ok":
                auth_state, auth_detail = _codex_auth(path)
        return EngineStatus(
            engine_id=engine_id, prereq_state=pre_state, prereq_detail=pre_detail,
            binary_state=state, binary=path, version=ver,
            auth_state=auth_state, auth_detail=auth_detail, notes=notes,
            install=(_NPM, "i", "-g", "@openai/codex")
            if state == "missing" and pre_state == "ok" else None,
            login=(path or "codex", "login") if state == "ok" else None,
        )

    if engine_id == "openai-http":
        auth_state, auth_detail = _openai_env_detail()
        # env 未設定は「壊れている」ではない（ローカル endpoint ならキー不要）ので
        # ✗ にしないで事実を note で添えるだけにする。
        notes = () if auth_detail else (("backend.status.note.openai_env_unset", {}),)
        return EngineStatus(
            engine_id=engine_id, auth_state=auth_state, auth_detail=auth_detail,
            notes=notes,
        )

    # mock（と未知の id）: 何も要らない
    return EngineStatus(engine_id=engine_id)


def check_all(engine_ids=None) -> list[EngineStatus]:
    """全ドライバ分。UI は並列に呼ぶので、ここは素直な逐次実装にしておく。"""
    if engine_ids is None:
        from llm_backend.engines import ENGINES
        engine_ids = [e.id for e in ENGINES]
    return [check_engine(eid) for eid in engine_ids]
