"""Claude サブ（2 つ目の Claude のアカウント）の認証。Qt 非依存・i18n 非依存。

``claude setup-token`` で作った長期トークンを、子の Claude Code に ``CLAUDE_CODE_OAUTH_TOKEN`` として渡す
（``auth_env``。起動のたびに呼ぶ）。トークンが無い・戻せない・形が違うときは ``AuthError`` で、
呼び出し側（``ClaudeCodeBackend._auth``）はプロセスを起動しない。メインの認証・別のエンジンには倒さない。

置き場（リポジトリ・同期ドライブ・R2 同期・agent_home の外）:

- Windows は ``~/.myanalysis/secrets/claude_sub.bin`` に DPAPI の blob。中身は、空白を除いたトークンの UTF-8 を
  DPAPI（CurrentUser・エントロピー無し）で暗号化した blob そのもの。DPAPI の blob は、この PC の同じ Windows の利用者
  でしか戻せない（トークンは PC ごとに入れる）。
- macOS はログインキーチェーン（generic password、service ``MyAnalysis-claude-sub``・account ``claude_sub``）で、
  ファイルは作らない。``/usr/bin/security`` の ``-i`` に stdin で渡し、argv に載せない。書いた後に読み直して確かめる。
  ``security`` の終了コード 44（項目が無い）は未設定、ほかの失敗は ``AuthError("keychain")``。
  既知の制約: SSH などでログインキーチェーンが検索リストに無いと、``find-generic-password`` は 44 を返し「未設定」と表示される。
- Linux 等（テストと開発用）は暗号化せず、0700 のフォルダの 0600 のファイルに書く。``/proc/mounts`` の無い
  macOS 以外の POSIX では判定できないので書かない。

ファイルに置く OS では、``~``（Windows は %USERPROFILE%）は PC ローカルである前提（MyAnalysis は agent_home・
config_share_state.json などの PC ローカルの状態も ``~/.myanalysis`` に置いている）。置き場が同期・ネットワークのマウントの
上か判定できないときも含めて、そうなら書かない（``synced_location``。``_on_synced_mount``。見かけのフォルダとリンクを辿った
実体の両方を見る）。それ以外の同期（検出できないもの）で写されても、DPAPI の blob は他の PC・利用者では戻せない。
対応するのは Windows と macOS。

claude は、設定ファイルの ``apiKeyHelper`` と ``env`` の認証の変数を ``CLAUDE_CODE_OAUTH_TOKEN`` より先に使う（2.1.301 の同梱コードの
認証の先の順: apiKeyHelper → ANTHROPIC_AUTH_TOKEN → CLAUDE_CODE_OAUTH_TOKEN → …。設定の env は起動時に環境変数として入る）。
子の cwd が相対パスでも、起動元の基準で絶対パスに直してから上のフォルダをたどる（``_child_cwds``）。
子の環境から ``AUTH_VARS`` を落としても設定ファイル側は残るので、``check_settings`` が claude の読む設定（利用者・cwd とその
上のフォルダの .claude・管理者の設定とポリシーのレジストリ）を見て、あれば起動しない（``settings_override``）。

``AuthError.code`` は GUI が i18n の文に変える鍵。``str(AuthError)`` は英語の中立な文で、
**トークンの値は決して含めない**（preflight・ModelList.detail と同じく、ここでは tr を使わない）。
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from common.fs_kind import is_fragile
from common.paths import atomic_write_bytes, repo_root

TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN_PREFIX = "sk-ant-oat"
_FILE_NAME = "claude_sub.bin"
_MAX_BYTES = 64 * 1024          # 置き場のファイルの上限（DPAPI の blob はトークンの数倍）
_TOKEN_MAX = 4096               # トークンの字数の上限
_TOKEN_RE = re.compile(r"[\x21-\x7e]+")     # 空白の無い ASCII の印字できる字
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_SETTINGS_MAX = 1024 * 1024     # claude の設定ファイルを読む上限
_PROC_MOUNTS = "/proc/mounts"   # POSIX のマウントの一覧（common.fs_kind が読む）
_SECURITY = "/usr/bin/security"
_KC_SERVICE = "MyAnalysis-claude-sub"
_KC_ACCOUNT = "claude_sub"
_KC_NOT_FOUND = 44            # errSecItemNotFound (-25300) の下位 8 ビット
_KC_LINE_MAX = 4094           # security -i の 1 行（改行を除く）の上限バイト数
_KC_TIMEOUT = 60.0            # 読み・保存・削除（ロック解除の確認に答える時間を見込む）
_KC_STATE_TIMEOUT = 5.0       # 状態（GUI スレッドから呼ぶ）

# 認証の先を決める変数。claude_code._build_env がサブの子の環境から落とし、claude の設定ファイルの env に
# あれば check_settings が起動を止める。ANTHROPIC_AUTH_TOKEN などは CLAUDE_CODE_OAUTH_TOKEN より先に効くので、
# 利用者が後でこれらを足しても、Claude サブがメインの側（や別の認証）に流れないようにする。
# 照合は大文字に揃えて行う（Windows の環境変数名は大小を区別しない）。
AUTH_VARS = frozenset({
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_UNIX_SOCKET",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "CLAUDE_CODE_OAUTH_SCOPES",
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_GATEWAY",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
})

_CHECK_MESSAGES = {
    "empty": "token is empty",
    "prefix": f"token must start with {TOKEN_PREFIX} (a claude setup-token token)",
    "chars": "token has invalid characters or is too long",
}


class AuthError(RuntimeError):
    """トークンが無い・読めない・形が違う・書けない・消せない。``code`` は GUI の i18n の鍵。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def token_path() -> Path:
    """置き場。テストが差し替えるので、このモジュールの中でも呼ぶ時点の global として引く。"""
    return Path.home() / ".myanalysis" / "secrets" / _FILE_NAME


def clean_token(text: str) -> str:
    """貼られた文から空白（改行を含む）を全部外す（端末からコピーすると途中に改行が入る）。"""
    return "".join(str(text).split())


def check_token(token: str) -> str | None:
    """形が正しくなければ理由のコード（"empty" / "prefix" / "chars"）。正しければ None。"""
    if not token:
        return "empty"
    if not token.startswith(TOKEN_PREFIX):
        return "prefix"
    if len(token) > _TOKEN_MAX or not _TOKEN_RE.fullmatch(token):
        return "chars"
    return None


# ----- macOS のキーチェーン（テストが _use_keychain / _security を差し替える） -----

def _use_keychain() -> bool:
    return sys.platform == "darwin"


def storage_kind() -> str:
    """置き場の種類。"keychain"（macOS）/ "dpapi"（Windows）/ "plain"（それ以外。平文 0600）。GUI が文を選ぶのに使う。"""
    if _use_keychain():
        return "keychain"
    if sys.platform == "win32":
        return "dpapi"
    return "plain"


def _security(args: list[str], *, stdin: str | None = None, timeout: float) -> tuple[int, str]:
    """``/usr/bin/security`` を実行して (終了コード, 標準出力)。投げない（起動できない・時間切れは (-1, "")）。

    標準エラーは返さない（地域化されるので判定に使わない）。
    """
    try:
        r = subprocess.run([_SECURITY, *args], input=stdin, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return -1, ""
    return r.returncode, r.stdout or ""


def _keychain_add_line(token: str) -> str:
    """``security -i`` に送る 1 行（末尾の改行を含む）。

    ``security`` の split_line は ``"`` か ``'`` で引用を始めて同じ字でだけ閉じ、``\\`` は次の 1 字をそのまま写すので、
    ``\\`` と ``"`` だけをエスケープして ``"`` で囲む。1 行が上限を超えると残りが別のコマンドとして実行されるので拒否する。
    """
    if any(c in token for c in "\r\n\0"):
        raise AuthError("chars", _CHECK_MESSAGES["chars"])
    escaped = token.replace("\\", "\\\\").replace('"', '\\"')
    body = f'add-generic-password -U -a "{_KC_ACCOUNT}" -s "{_KC_SERVICE}" -w "{escaped}"'
    if len(body.encode("utf-8")) > _KC_LINE_MAX:
        raise AuthError("chars", _CHECK_MESSAGES["chars"])
    return body + "\n"


def _keychain_error(rc: int) -> AuthError:
    return AuthError("keychain", f"cannot access the macOS Keychain (security exit {rc})")


def _kc_find_args(*, secret: bool) -> list[str]:
    args = ["find-generic-password", "-s", _KC_SERVICE, "-a", _KC_ACCOUNT]
    if secret:
        args.append("-w")
    return args


def read_token() -> str:
    """置き場のトークン。無い・読めない・形が違えば AuthError。ワーカースレッドからも呼ぶ。"""
    if _use_keychain():
        rc, out = _security(_kc_find_args(secret=True), timeout=_KC_TIMEOUT)
        if rc == 0:
            token = clean_token(out)
            if check_token(token) is not None:
                raise AuthError("corrupt", "claude-sub keychain item is corrupt")
            return token
        if rc == _KC_NOT_FOUND:
            raise AuthError("not_set", "claude-sub token is not set")
        raise _keychain_error(rc)
    path = token_path()
    try:
        with open(path, "rb") as f:
            blob = f.read(_MAX_BYTES + 1)
    except FileNotFoundError:
        raise AuthError("not_set", "claude-sub token is not set") from None
    except OSError as e:
        raise AuthError("undecryptable", f"cannot read the claude-sub token file ({type(e).__name__})") from None
    if len(blob) > _MAX_BYTES:
        raise AuthError("too_large", "claude-sub token file is too large")
    try:
        token = clean_token(_unprotect(blob).decode("utf-8"))
    except (OSError, UnicodeDecodeError):
        raise AuthError("undecryptable", "cannot decrypt the claude-sub token "
                        "(written by another user or PC, or damaged)") from None
    if check_token(token) is not None:
        raise AuthError("corrupt", "claude-sub token file is corrupt")
    return token


def token_state() -> str:
    """"ok" / "missing"（ファイルが無い）/ "bad"（在るが使えない）。投げない。

    キーチェーンでは値を読まない（``-w`` を付けない。属性はロックされていても確認なしで引けるので、GUI スレッドから
    呼んでも止まらない）。その "ok" は「項目がある」だけで、中身の形は確かめない（壊れていれば送信時の
    ``read_token`` が ``corrupt``）。
    """
    if _use_keychain():
        rc, _ = _security(_kc_find_args(secret=False), timeout=_KC_STATE_TIMEOUT)
        if rc == 0:
            return "ok"
        if rc == _KC_NOT_FOUND:
            return "missing"
        return "bad"
    try:
        read_token()
    except AuthError as e:
        return "missing" if e.code == "not_set" else "bad"
    except Exception:
        return "bad"
    return "ok"


def auth_env() -> dict[str, str]:
    """Claude サブの子の claude に足す環境。トークンが無い・読めなければ AuthError（起動のたびに呼ぶ）。"""
    return {TOKEN_ENV: read_token()}


def save_token(text: str) -> None:
    """貼られたトークンを置き場に書く（空白を外し、形を確かめてから）。書けなければ AuthError。"""
    token = clean_token(text)
    reason = check_token(token)
    if reason is not None:
        raise AuthError(reason, _CHECK_MESSAGES[reason])
    if _use_keychain():
        # トークンは argv に載せない（ps・EDR から見える）。-T も付けない（作った security 自身が信頼される）。
        line = _keychain_add_line(token)
        _security(["-i"], stdin=line, timeout=_KC_TIMEOUT)      # 成否は読み直しで判断する
        rc, out = _security(_kc_find_args(secret=True), timeout=_KC_TIMEOUT)
        if rc != 0:
            raise _keychain_error(rc)
        if clean_token(out) != token:
            raise AuthError("keychain", "macOS Keychain read-back did not match the saved token")
        return
    path = token_path()
    try:
        inside = path.resolve().is_relative_to(repo_root().resolve())
    except OSError:
        inside = True                       # 判定できなければ書かない側に倒す
    if inside:
        raise AuthError("inside_repo", "claude-sub token path is inside the repository; not written")
    try:
        blob = _protect(token.encode("utf-8"))
        path.parent.mkdir(parents=True, exist_ok=True)
        # 同期・ネットワークのマウント（判定できなければ書かない）。見かけのフォルダと、リンク・junction を
        # 辿った実体のフォルダの両方を見る（fs_kind は字句のパスで判定し、リンクを辿らない）。
        real_parent = _real_dir(path.parent)
        if real_parent is None or _on_synced_mount(path.parent) or _on_synced_mount(real_parent):
            raise AuthError("synced_location", "claude-sub token path is on a synced mount; not written")
        if sys.platform != "win32":
            os.chmod(path.parent, 0o700)
        atomic_write_bytes(path, blob)
        if sys.platform != "win32":
            os.chmod(path, 0o600)
    except OSError as e:                    # MountWriteError も OSError
        raise AuthError("write_failed", f"cannot write the claude-sub token file ({type(e).__name__})") from None


def _mounts_readable() -> bool:
    """POSIX で /proc/mounts を読めるか。投げない。"""
    try:
        with open(_PROC_MOUNTS, "rb"):
            return True
    except OSError:
        return False


def _real_dir(directory: Path) -> Path | None:
    """リンク・junction を辿った実体のフォルダ。辿れなければ None（書かない側）。投げない。"""
    try:
        return directory.resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def _on_synced_mount(directory: Path) -> bool:
    """置き場が同期・ネットワークのマウントの上か。判定できなければ True（書かない側）。

    Windows は common.fs_kind（ボリュームの FS 名。UNC・ネットワークドライブ・NTFS などでない FS・判定の失敗は fragile）。
    POSIX は /proc/mounts を読めるときだけ fs_kind（FUSE・CIFS・NFS などは fragile）を使う。読めないとき fs_kind は
    local を返すので、ここで判定できないとして True にする（macOS 以外の POSIX。macOS はキーチェーンを使うのでここを通らない）。
    """
    if sys.platform != "win32" and not _mounts_readable():
        return True
    return is_fragile(directory)


def delete_token() -> None:
    """置き場のトークンを消す（無ければ何もしない）。消せなければ AuthError。"""
    if _use_keychain():
        rc, _ = _security(["delete-generic-password", "-s", _KC_SERVICE, "-a", _KC_ACCOUNT], timeout=_KC_TIMEOUT)
        if rc not in (0, _KC_NOT_FOUND):
            raise _keychain_error(rc)
    try:
        token_path().unlink(missing_ok=True)
    except OSError as e:
        raise AuthError("delete_failed", f"cannot delete the claude-sub token file ({type(e).__name__})") from None


# ----- claude の設定が認証を変えていないか（テストが _claude_config_dirs などを差し替える） -----

def _child_cwds(cwd: Path) -> list[Path]:
    """子の claude の cwd。相対なら起動元の os.getcwd() 基準の絶対パス（Popen の cwd= と同じ解決）。
    リンクを辿った形が違えばそれも足す（どちらの親の設定も見る＝使わない側に倒す）。"""
    absolute = Path(os.path.abspath(cwd))
    out = [absolute]
    try:
        real = absolute.resolve()
    except OSError:
        real = absolute
    if real != absolute:
        out.append(real)
    return out


def _claude_config_dirs(cwd: Path) -> list[Path]:
    """claude の利用者の設定のフォルダ（env CLAUDE_CONFIG_DIR、無ければ ~/.claude）。

    CLAUDE_CONFIG_DIR は子にそのまま渡るので、相対なら子の cwd 基準で解く（子の claude と同じ基準）。
    ``~`` で始まる値は展開した形も見る。
    """
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if not env:
        return [Path.home() / ".claude"]
    out = [Path(os.path.abspath(cwd)) / env]       # env が絶対ならそのまま
    expanded = Path(env).expanduser()
    if expanded.is_absolute() and expanded not in out:
        out.append(expanded)
    return out


def _managed_settings_files() -> list[Path]:
    """管理者の設定（managed-settings.json）の置き場。"""
    if sys.platform == "win32":
        return [Path("C:/Program Files/ClaudeCode/managed-settings.json"),
                Path("C:/ProgramData/ClaudeCode/managed-settings.json")]
    if sys.platform == "darwin":
        return [Path("/Library/Application Support/ClaudeCode/managed-settings.json")]
    return [Path("/etc/claude-code/managed-settings.json")]


def _policy_registry_present() -> bool:
    """Windows のポリシーのレジストリ（HKLM / HKCU の SOFTWARE\\Policies\\ClaudeCode）があるか。判定できなければ True。"""
    if sys.platform != "win32":
        return False
    import winreg
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            winreg.CloseKey(winreg.OpenKey(hive, r"SOFTWARE\Policies\ClaudeCode"))
            return True
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return False


def _ancestor_dirs(cwd: Path) -> list[Path]:
    """プロジェクトの設定を探すフォルダ（cwd とその上の全部。claude が読まない上のフォルダも見る＝使わない側に倒す）。"""
    return [cwd, *cwd.parents]


def _settings_files(cwd: Path) -> list[Path]:
    files = [d / "settings.json" for d in _claude_config_dirs(cwd)]
    for base in _child_cwds(cwd):
        for d in _ancestor_dirs(base):
            files += [d / ".claude" / "settings.json", d / ".claude" / "settings.local.json"]
    files += _managed_settings_files()
    out: list[Path] = []
    for f in files:
        if f not in out:
            out.append(f)
    return out


def _file_override(path: Path) -> str | None:
    """1 つの設定ファイルが認証を変えていればその説明（値は含めない）。無い・変えていなければ None。"""
    try:
        with open(path, "rb") as f:
            raw = f.read(_SETTINGS_MAX + 1)
    except FileNotFoundError:
        return None
    except OSError as e:
        return f"{path} (unreadable: {type(e).__name__})"
    if len(raw) > _SETTINGS_MAX:
        return f"{path} (too large)"
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except ValueError:
        return f"{path} (not valid JSON)"
    if not isinstance(data, dict):
        return f"{path} (not a JSON object)"
    if data.get("apiKeyHelper"):
        return f"{path} (apiKeyHelper)"
    env = data.get("env")
    if isinstance(env, dict):
        hits = sorted(k for k in env if isinstance(k, str) and k.upper() in AUTH_VARS)
        if hits:
            return f"{path} (env: {', '.join(hits)})"
    return None


def settings_override(cwd: Path) -> str | None:
    """claude が読む設定に、サブのトークンより先に効く認証（apiKeyHelper・env の認証の変数）があればその場所。

    ``cwd`` は子に渡す cwd そのもの（相対でもよい。ここで子と同じ基準の絶対パスに直す。_auth と preflight が
    同じ値を渡すので、両方とも同じ場所を見る）。

    説明は中立な英語（ファイルのパスと項目の名前。値は含めない）。読めない・JSON でない設定も「ある」として返す
    （確かめられないものは使わない側に倒す）。無ければ None。投げない。
    """
    try:
        if _policy_registry_present():
            return "Windows policy registry (SOFTWARE\\Policies\\ClaudeCode)"
        for path in _settings_files(Path(cwd)):
            found = _file_override(path)
            if found is not None:
                return found
    except Exception as e:
        return f"claude settings could not be checked ({type(e).__name__})"
    return None


def check_settings(cwd: Path) -> None:
    """``settings_override`` があれば AuthError（起動しない）。cwd は子の claude の cwd。"""
    found = settings_override(cwd)
    if found is not None:
        raise AuthError("settings_override", f"claude settings would override the claude-sub token: {found}")


# ----- DPAPI（テストが _protect / _unprotect を差し替える） -----

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    """CryptProtectData / CryptUnprotectData（CurrentUser・エントロピー無し・UI 無し）。失敗は OSError。"""
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buf = ctypes.create_string_buffer(data, len(data))
    src = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_uint32, ctypes.POINTER(_Blob)]
    fn.restype = ctypes.c_int
    # 説明の文字列: Protect は空文字（.NET と同じ）、Unprotect は受け取らない（NULL）。
    descr = ctypes.c_wchar_p("") if protect else None
    if not fn(ctypes.byref(src), descr, None, None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        err = ctypes.get_last_error()
        raise OSError(err, ctypes.FormatError(err).strip() or f"DPAPI error {err}")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def _protect(data: bytes) -> bytes:
    if sys.platform == "win32":
        return _dpapi(data, protect=True)
    return bytes(data)


def _unprotect(blob: bytes) -> bytes:
    if sys.platform == "win32":
        return _dpapi(blob, protect=False)
    return bytes(blob)
