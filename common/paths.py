"""Filesystem path helpers. All paths are derived from __file__ so the module
location determines repo root. Move this file → repo_root changes accordingly."""
import contextlib
import json
import os
import tempfile
import time
from pathlib import Path

# Windows のファイル名に使えない文字。パス区切り (/ \) と制御文字は別途・汎用で拒否。
_WINDOWS_FORBIDDEN = set('<>:"|?*')

# Windows 予約デバイス名 (大文字小文字を区別しないため小文字でも拒否)。
_WINDOWS_RESERVED = frozenset(
    {
        "con", "prn", "aux", "nul",
        "com1", "com2", "com3", "com4", "com5",
        "com6", "com7", "com8", "com9",
        "lpt1", "lpt2", "lpt3", "lpt4", "lpt5",
        "lpt6", "lpt7", "lpt8", "lpt9",
    }
)


def validate_identifier_name(name: str, *, check_reserved: bool = True) -> None:
    """Raise ValueError if name is unsafe as a dataset / analysis name.

    拒否リスト方式。英大小文字・非ASCII (日本語等)・任意位置の数字・'-'・'_'
    は許可し、本当に危険なものだけ拒否する。

    check_reserved=True は解析名用 = <dataset_dir>/analyses/<name>/ という実フォルダに
    なるため、汎用ハイジーンに加えて Windows のファイル名規則 (禁止文字・末尾ドット・
    予約デバイス名) も適用する。
    check_reserved=False はデータセット名用 = DATASETS の dict キーにしかならず
    パスにならないので、汎用ハイジーンのみ。
    """
    from common.i18n import tr
    if not name:
        raise ValueError(tr("validate.empty"))
    if "/" in name or "\\" in name:
        raise ValueError(tr("validate.path_sep", name=name))
    if name in (".", "..") or name.startswith("."):
        raise ValueError(tr("validate.leading_dot", name=name))
    for c in name:
        if c.isspace():
            raise ValueError(tr("validate.whitespace", name=name))
        if ord(c) < 32 or ord(c) == 127:
            raise ValueError(tr("validate.control_char", name=name))
    if check_reserved:
        bad = sorted(set(name) & _WINDOWS_FORBIDDEN)
        if bad:
            raise ValueError(
                tr("validate.forbidden_char", chars="".join(bad), name=name)
            )
        if name.endswith("."):
            raise ValueError(tr("validate.trailing_dot", name=name))
        if name.lower() in _WINDOWS_RESERVED:
            raise ValueError(tr("validate.reserved_name", name=name))


def validate_name(name: str) -> None:
    """Raise ValueError if name is not a simple directory name.

    Rejects empty strings, path separators (/ \\), relative-path
    components (. and ..), and NUL bytes.  This prevents constructing
    paths outside the analysis output tree.
    """
    from common.i18n import tr
    if not name or "/" in name or "\\" in name or name in (".", "..") or "\0" in name:
        raise ValueError(tr("validate.simple_name", name=name))


def repo_root() -> Path:
    """Return the repo root, derived from this file's location.

    Layout invariant: common/paths.py lives at <repo>/common/paths.py.
    """
    return Path(__file__).resolve().parent.parent


def i18n_dir() -> Path:
    """Return <repo>/i18n/ (committed catalogs; not created)."""
    return repo_root() / "i18n"


def safe_resolve(path) -> Path:
    """resolve() が使えない FS でも死なない絶対パス正規化（.resolve() の drop-in 代替）。

    Path.resolve() / os.path.realpath は Windows で GetFinalPathNameByHandle を呼ぶが、
    WinFsp/rclone/一部のネットワークマウントはこれを実装しておらず
    OSError [WinError 1005]「このボリュームは認識可能なファイルシステムではありません」を
    投げる。通常の FS では resolve() の結果をそのまま返し（symlink 解決・大文字小文字の
    正準化を保つ＝既存挙動・既存テスト不変）、resolve() が OSError のときだけ
    os.path.abspath（純 lexical 正規化：絶対化＋'..' の字句畳み込み、FS 問い合わせ無し）に
    フォールバックしてマウント上でも動く。ここが置換する封じ込めチェックには lexical 正規化で
    十分であり、symlink を辿らない分だけ（マウント外へ逃げる work_dir に対して）むしろ堅牢。
    """
    p = Path(path)
    try:
        return p.resolve()
    except OSError:
        return Path(os.path.abspath(p))


def atomic_write_text(path, text: str, *, encoding: str = "utf-8") -> None:
    """一意 tmp + os.replace による atomic 書込（固定名 tmp を使わない）。

    固定名 `<target>.json.tmp` 等は rclone/WinFsp の VFS write-back キャッシュ上でゴースト化し、
    次の open(fixed_tmp,'w') が FileExistsError を起こして以後の書込を恒久ブロックし得る。
    tempfile.mkstemp は対象ディレクトリ内に O_CREAT|O_EXCL で毎回新しい一意名の tmp を作るので、
    残留 tmp が次の書込をブロックすることは原理的に起きない。

    改行変換は Path.write_text と同じ既定（newline=None: 書込時 '\\n' → os.linesep）に合わせ、
    正常 FS では書き出しバイトは従来の固定名パターンと byte-identical。親ディレクトリはここでは
    作らない（呼び出し側が従来どおり保証する。mkstemp は既存ディレクトリを要求＝旧 tmp.write_text と
    同条件）。生成ファイルの POSIX mode は mkstemp 既定の 0600 になる（旧パターンは umask 依存で
    通常 0644）が、単一ユーザの state/JSON/TOML ファイルでは無害・Windows/rclone マウントでは
    mode bit は無視されるため差異なし（前提の詳細は Issue #69「前提と破綻時の影響」節）。

    例外安全: mkstemp を try 内に置き、fd の所有権を追跡する。os.fdopen が fd 所有権を取る前に
    失敗した場合は fd を best-effort で close、成功後は with が close するので二重 close しない
    （閉じた fd 番号の再利用による誤 close を避けるため sentinel `fd = None` で判定）。tmp の削除も
    OSError を握って best-effort とし、掃除失敗が元の write/replace 例外をマスクしないようにする。
    """
    path = Path(path)
    fd = None   # None = fd を所有していない（未取得 or fdopen が所有権を取得済み）
    tmp = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent, prefix=path.name + ".", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        f = os.fdopen(fd, "w", encoding=encoding)   # 成功で f が fd を所有
        fd = None                                   # 所有権が f に移った
        with f:                                     # write の成否に関わらず f が fd を閉じる
            f.write(text)                           # newline 既定=None（write_text と同じ）
            f.flush()
            with contextlib.suppress(OSError):      # fsync 非対応マウントでも write を壊さない
                os.fsync(f.fileno())                # cache→バックエンド upload の durability を促す
        os.replace(tmp, path)
    except BaseException:
        if fd is not None:                          # fdopen 前に失敗 → fd は未クローズ
            with contextlib.suppress(OSError):
                os.close(fd)
        if tmp is not None:                         # 自分の tmp は残さない（掃除失敗で元例外を隠さない）
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
        raise


def atomic_write_bytes(path, data: bytes) -> None:
    """atomic_write_text のバイナリ版（一意 tmp + os.replace）。

    PNG 等のバイナリ出力を rclone/WinFsp マウント上へ安全に書くための chokepoint。
    matplotlib/PIL に直接パスを渡すと Image.save → os.path.realpath で WinError 1005 に
    なるため（common.mount_compat 参照）、呼び出し側は BytesIO へ描画してから生バイトを
    ここへ渡す。tempfile.mkstemp の一意名 + os.replace はマウント上で動作実績があり
    （Issue #69）、固定名 tmp のゴースト衝突も起きない。write は逐次バイト書き込みのみで
    realpath を経由しない。例外安全（fd 所有権追跡・tmp の best-effort 掃除で元例外を
    マスクしない）は atomic_write_text と同一。
    """
    path = Path(path)
    fd = None
    tmp = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent, prefix=path.name + ".", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        f = os.fdopen(fd, "wb")                      # 成功で f が fd を所有
        fd = None                                   # 所有権が f に移った
        with f:                                     # write の成否に関わらず f が fd を閉じる
            f.write(data)
            f.flush()
            with contextlib.suppress(OSError):      # fsync 非対応マウントでも write を壊さない
                os.fsync(f.fileno())                # cache→バックエンド upload の durability を促す
        os.replace(tmp, path)
    except BaseException:
        if fd is not None:                          # fdopen 前に失敗 → fd は未クローズ
            with contextlib.suppress(OSError):
                os.close(fd)
        if tmp is not None:                         # 自分の tmp は残さない（掃除失敗で元例外を隠さない）
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
        raise


def read_json_classified(path, *, retries: int = 3, delay: float = 0.05):
    """JSON ファイルを三値で読む: ('ok', dict) / ('absent', None) / ('unreadable', None)。

    同期ドライブ（rclone/WinFsp）では「一瞬の空/未同期/再DL 失敗」が起きうる。それを
    「本当に無い（=既定値で作り直してよい）」と混同すると、transient なグリッチが恒久
    データ損失に化ける（config_share._read_file_state と同じ規律をここで汎用化する）。

    'absent'     = FileNotFoundError。primary が本当に無い＝新規作成/上書き安全。
    'unreadable' = ファイルは在る(or 判定不能)が retries 回試しても UTF-8+JSON+dict に
                   parse できない（0byte/破損/OSError/同期途中）。呼び出し側はこれを
                   既定値で上書きしてはならない。
    retries/delay は同期ラグ・replace 直後の一瞬の空を吸収する。read が最初から成功する
    正常パスでは sleep しない（遅延を足さない）。never raise。
    """
    path = Path(path)
    for attempt in range(retries):
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:          # OSError のサブクラス — 先に捕捉
            return ("absent", None)
        except (OSError, ValueError):      # ValueError=UnicodeDecodeError も含む → 在るが読めず
            text = ""
        if text:
            try:
                data = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                data = None
            if isinstance(data, dict):
                return ("ok", data)
        if attempt < retries - 1:
            time.sleep(delay)
    return ("unreadable", None)


def bak_path(path) -> Path:
    """path のサイドカー・バックアップ名（例: meta.json → meta.json.bak）。

    末尾へ '.bak' を付けるだけ（拡張子置換ではない）ので、'*.json' グロブには一致しない
    ＝件数カウント/列挙で二重計上・誤ロードされない。
    """
    p = Path(path)
    return p.with_name(p.name + ".bak")


def durable_write_json(path, data) -> None:
    """非再計算 JSON を primary＋サイドカー '.bak' の 2 コピーで atomic(＋fsync) 書込。

    primary → .bak の順に書く: 途中クラッシュでも primary は健全で、.bak が primary より
    新しくなることはない（durable_read_json が古い .bak で新しい primary を巻き戻さない
    ための不変条件）。同期ドライブで primary が evict＋upload 失敗で失われても .bak から
    回復できる。小さな JSON 前提（2× 書込のコストは無視できる）。
    """
    text = json.dumps(data, ensure_ascii=False, indent=2)
    p = Path(path)
    atomic_write_text(p, text)
    atomic_write_text(bak_path(p), text)


def durable_read_json(path, *, retries: int = 3, delay: float = 0.05):
    """primary→.bak の順で読む純読取（**書込しない**）。never raise。

    返り値: ('ok', dict) / ('absent', None) / ('recovered', dict) / ('unreadable', None)。
    primary が読めれば 'ok'。primary が読めない（absent/unreadable）ときは .bak を見る:
    .bak が読めれば 'recovered'（primary が evict/削除/破損でも .bak が生きていれば回復）。
    .bak も駄目なら、primary が本当に無ければ 'absent'（新規作成/上書き安全）、primary が
    在るが読めなければ 'unreadable'（呼び出し側は上書き禁止）。

    read 中に primary を書き換えないので、(a) 非ロックな read でのレース、(b) 新しい
    primary を古い .bak で巻き戻す事故、を両方回避する。自己修復は呼び出し側の次回
    durable_write_json（その exclusive_lock 内）が担う。
    """
    p = Path(path)
    status, data = read_json_classified(p, retries=retries, delay=delay)
    if status == "ok":
        return ("ok", data)
    # primary が absent/unreadable → durable な .bak を試す。
    bstatus, bdata = read_json_classified(bak_path(p), retries=retries, delay=delay)
    if bstatus == "ok":
        return ("recovered", bdata)   # primary が失われても .bak が生存
    if status == "absent":
        return ("absent", None)       # primary が本当に無く .bak も使えない → 新規
    return ("unreadable", None)       # primary は在るが読めず .bak も駄目 → 上書き禁止
