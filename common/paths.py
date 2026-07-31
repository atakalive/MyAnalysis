"""Filesystem path helpers. All paths are derived from __file__ so the module
location determines repo root. Move this file → repo_root changes accordingly.

## 書込の規律（Issue #96 — 読む前に必ずここを読むこと）

**同期マウント（rclone/WinFsp）上で rename-into-place をしてはならない。**
`os.replace(tmp, target)` はマウント層では成功を返しながら rclone のキャッシュ層で
`Access is denied` になり、rclone が cache item を破棄して**ファイルが 0 バイトに見える**。
実測では高い頻度で発生し、例外は一切上がらない（詳細は common/fs_kind.py）。

そのため `atomic_write_text` / `atomic_write_bytes` は書込先の FS 種別で戦略を切り替える:

    fragile FS (rclone/WinFsp/ネットワーク) → in-place write（rename しない）+ read-back 検証 + リトライ
    local FS   (NTFS/ReFS/ext4 …)          → 従来どおり mkstemp + os.replace（真に atomic）

**新しい書込経路を足すときは必ずこの 2 関数を通すこと。** 生の `open(path,"w")` /
`Path.write_text` / `QPixmap.save(path)` / `fig.savefig(path)` をデータセットディレクトリに
向けてはならない。
"""
import contextlib
import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path, PureWindowsPath

from common import fs_kind

_log = logging.getLogger(__name__)

# 検証失敗時のリトライ間隔（秒）。合計 5 試行 / 最悪 ~6.2 秒。
# 実測の根拠: rclone のログ上、cache rename の失敗は**すべて**後続の試行で成功しており
# （中央値 8 秒）、失敗そのものが宛先 cache item を evict するため直後の再試行が通りやすい。
# 対話パスを長時間止めないため合計 10 秒未満に収める。
_VERIFY_BACKOFF = (0.0, 0.15, 0.5, 1.5, 4.0)

# 書込戦略のキルスイッチ: auto（既定・FS で判定）/ replace（旧挙動）/ inplace（常に in-place）
_ENV_STRATEGY = "MYANALYSIS_WRITE_STRATEGY"


class MountWriteError(OSError):
    """書込が検証を通らなかった（マウントが成功を偽った / 部分書込 / cache 破棄）。

    `OSError` のサブクラスにしてあるのは、既存の `except OSError` ハンドラが従来どおり
    動いて GUI が落ちないようにするため。**握り潰されても失われない**よう、送出前に必ず
    failure sink とログへ流す。
    """

    def __init__(self, path, *, attempts: int, detail: str):
        self.path = str(path)
        self.attempts = attempts
        self.detail = detail
        super().__init__(
            f"write could not be verified after {attempts} attempts: {path} — {detail}")


_failure_sink = None


def set_write_failure_sink(fn) -> None:
    """書込検証の失敗を受け取るコールバックを登録する（GUI が起動時に 1 回登録）。

    Qt 非依存にするため単なる callable。payload は dict（path / attempts / detail / strategy）。
    CLI では未登録＝ログのみ。sink 自身の例外は握る（通知の失敗で書込経路を壊さない）。
    """
    global _failure_sink
    _failure_sink = fn


def _report_failure(payload: dict) -> None:
    _log.error("mount write verification failed: %s", payload)
    if _failure_sink is not None:
        with contextlib.suppress(Exception):
            _failure_sink(payload)


def _strategy_for(path) -> str:
    """'inplace' | 'replace'。env のキルスイッチが FS 判定に優先する。"""
    env = os.environ.get(_ENV_STRATEGY, "").strip().lower()
    if env in ("replace", "inplace"):
        return env
    return "inplace" if fs_kind.is_fragile(path) else "replace"

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


def validate_relpath(relpath: str) -> tuple[str, ...]:
    """work_dir 配下へ書くための相対パスを検証し、正規化した構成要素を返す。

    `validate_name` が「区切りを一切含まない単一名」を強制するのに対し、こちらは
    サブディレクトリを許す（save_text が任意の拡張子とサブディレクトリを受けるため）。

    受理: "notes.md" / "reports/summary.md" / "reports\\summary.md" / "日本語/メモ.md"
          / "summary report.md" ——空白入りのファイル名は無害なので許可する。
          解析名を縛る validate_identifier_name との意図的な差異（あちらは名前が
          モジュール識別子と CLI トークンになるので空白を一律拒否している）。
    拒否: 空・空白のみ / ".." / 絶対・ルート相対・ドライブ相対・UNC / NUL・制御文字
          / Windows 禁止文字 <>:"|?* / 先頭ドット（dataset_summary が dotfile を読み飛ばす
          ので、書いても書いた本人から見えない）/ 末尾ドット・末尾空白（Windows が黙って
          落とすため、返した Path と実体が食い違う）/ **拡張子が付いていても** Windows
          予約デバイス名（"nul.txt" は NUL デバイスであってファイルではない）。

    解析を PureWindowsPath で行うので、判定は POSIX 上でも Windows と同一になる
    （dataset_config.get_work_dir と同じ規律）。副産物として "." と重複区切りは解析時に
    畳まれ、"\\" も区切りとして扱われる（プラットフォーム差を作らない）。

    Returns:
        正規化した構成要素のタプル（例: ("reports", "summary.md")）。
        「文字列 A を検証して文字列 B を join する」型の取り違えを構造的に防ぐため、
        呼び出し側はこの戻り値を使って join すること（→ resolve_under）。

    Raises:
        ValueError: 上記のいずれかに該当。
    """
    from common.i18n import tr
    if not isinstance(relpath, str) or not relpath.strip():
        raise ValueError(tr("relpath.empty", relpath=relpath))
    pw = PureWindowsPath(relpath)
    if pw.drive:            # "C:foo"（ドライブ相対）と UNC "\\\\srv\\share" の両方
        raise ValueError(tr("relpath.drive_rel", relpath=relpath))
    if pw.root:             # "/foo" / "\\foo"
        raise ValueError(tr("relpath.root_rel", relpath=relpath))
    parts = pw.parts
    if not parts:           # "." は解析時に畳まれて空になる
        raise ValueError(tr("relpath.empty", relpath=relpath))
    if ".." in parts:
        raise ValueError(tr("relpath.dotdot", relpath=relpath))
    for part in parts:
        for c in part:
            if ord(c) < 32 or ord(c) == 127:    # NUL を含む制御文字
                raise ValueError(tr("validate.control_char", name=part))
        # ':' はここで捕まえる。"sum:mary.md" の drive は空なので上の drive 判定に乗らない。
        bad = sorted(set(part) & _WINDOWS_FORBIDDEN)
        if bad:
            raise ValueError(
                tr("validate.forbidden_char", chars="".join(bad), name=part)
            )
        if part.startswith("."):
            raise ValueError(tr("validate.leading_dot", name=part))
        if part[-1] in ". ":
            raise ValueError(tr("relpath.trailing_dot_space", name=part))
        # 予約デバイス名は拡張子が付いても device のまま（"nul.txt" は NUL に化ける）。
        # _WINDOWS_RESERVED を名前全体と比べるだけでは取り逃すので stem で判定する。
        if part.split(".")[0].lower() in _WINDOWS_RESERVED:
            raise ValueError(tr("validate.reserved_name", name=part))
    return parts


def repo_root() -> Path:
    """Return the repo root, derived from this file's location.

    Layout invariant: common/paths.py lives at <repo>/common/paths.py.
    """
    return Path(__file__).resolve().parent.parent


def i18n_dir() -> Path:
    """Return <repo>/i18n/ (committed catalogs; not created)."""
    return repo_root() / "i18n"


def pycache_prefix() -> Path:
    """`PYTHONPYCACHEPREFIX` に入れるローカルのバイトコード退避先（Issue #96）。

    同期マウント上の .py を import すると CPython が `__pycache__/*.pyc` を tmp+rename で
    書き、rclone のキャッシュ層で rename が失敗して 0 バイト化する経路に乗る（実測で
    rename 失敗の 16% が .pyc）。バイトコードを丸ごとローカルへ逃がせば、キャッシュの
    利点を捨てずにマウントへの書込だけを止められる。
    """
    return repo_root() / "data" / "pycache"


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


def resolve_under(root, relpath: str) -> Path:
    """validate_relpath + 封じ込め再チェック。root 配下の絶対 Path を返す。

    **mkdir はしない。** 拒否したパスの途中ディレクトリだけが残る事故を防ぐため、
    呼び出し側は必ず「resolve_under を通す → それから mkdir」の順にすること。

    封じ込めは safe_resolve(...).is_relative_to(safe_resolve(root)) で再確認する
    （dataset_config.get_work_dir / analysis_file と同じ defense-in-depth）。
    validate_relpath は字句的な検証しかしないので、root 配下の symlink 経由で外へ
    出るケースはここでしか落とせない。

    Raises:
        ValueError: relpath が不正、または解決結果が root の外。
    """
    from common.i18n import tr
    parts = validate_relpath(relpath)
    root = Path(root)
    target = root.joinpath(*parts)
    if not safe_resolve(target).is_relative_to(safe_resolve(root)):
        raise ValueError(tr("relpath.escape", root=root, relpath=relpath))
    return target


def _write_via_replace(path: Path, data, *, binary: bool, encoding, newline) -> None:
    """一意 tmp + os.replace（local FS 専用）。

    固定名 `<target>.json.tmp` 等は rclone/WinFsp の VFS write-back キャッシュ上でゴースト化し、
    次の open(fixed_tmp,'w') が FileExistsError を起こして以後の書込を恒久ブロックし得る
    （Issue #69）。tempfile.mkstemp は対象ディレクトリ内に O_CREAT|O_EXCL で毎回新しい一意名の
    tmp を作るので、残留 tmp が次の書込をブロックすることは原理的に起きない。

    例外安全: mkstemp を try 内に置き、fd の所有権を追跡する。os.fdopen が fd 所有権を取る前に
    失敗した場合は fd を best-effort で close、成功後は with が close するので二重 close しない
    （閉じた fd 番号の再利用による誤 close を避けるため sentinel `fd = None` で判定）。tmp の削除も
    OSError を握って best-effort とし、掃除失敗が元の write/replace 例外をマスクしないようにする。
    """
    fd = None   # None = fd を所有していない（未取得 or fdopen が所有権を取得済み）
    tmp = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent, prefix=path.name + ".", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        f = (os.fdopen(fd, "wb") if binary
             else os.fdopen(fd, "w", encoding=encoding, newline=newline))
        fd = None                                   # 所有権が f に移った
        with f:                                     # write の成否に関わらず f が fd を閉じる
            f.write(data)
            f.flush()
            with contextlib.suppress(OSError):      # fsync 非対応マウントでも write を壊さない
                os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if fd is not None:                          # fdopen 前に失敗 → fd は未クローズ
            with contextlib.suppress(OSError):
                os.close(fd)
        if tmp is not None:                         # 自分の tmp は残さない（掃除失敗で元例外を隠さない）
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
        raise


def _write_in_place(path: Path, data, *, binary: bool, encoding, newline) -> None:
    """rename を使わない直接上書き（fragile FS 専用）。

    truncate 窓（open〜write 完了の間にプロセスが死ぬと 0 バイトが残る）を最小化するため、
    **全バイトを組み立ててから単一の write() で流す**（逐次 write 禁止）。窓は sub-ms。
    残る窓は (a) primary の後に書く `.bak`、(b) 呼び出し側の read-back 検証とリトライ、
    (c) durable_read_json の newest-wins フォールバックで覆う。
    """
    mode = "wb" if binary else "w"
    kw = {} if binary else {"encoding": encoding, "newline": newline}
    with open(path, mode, **kw) as f:
        f.write(data)
        f.flush()
        with contextlib.suppress(OSError):
            os.fsync(f.fileno())


def _read_back_matches(path: Path, data, *, binary: bool, encoding, newline) -> tuple[bool, str]:
    """書いた内容が読み戻せるか。→ (一致したか, 不一致の説明)。

    テキストは**書込と同じ encoding/newline で読む**ので、newline 変換が対称になり
    CRLF/LF の差で偽陽性を出さない。バイナリは大きい場合だけ sha256 に落とす
    （PNG のために 2 コピーをメモリに置かない）。
    """
    try:
        if binary:
            got = path.read_bytes()
            if len(got) != len(data):
                return (False, f"size {len(got)} != {len(data)}")
            if len(data) > (4 << 20):
                a = hashlib.sha256(got).hexdigest()
                b = hashlib.sha256(data).hexdigest()
                return (a == b, "" if a == b else f"sha256 {a[:16]} != {b[:16]}")
            return (got == data, "" if got == data else "byte mismatch")
        with open(path, "r", encoding=encoding, newline=newline) as f:
            got = f.read()
        if got == data:
            return (True, "")
        return (False, f"text mismatch (got {len(got)} chars, want {len(data)})")
    except OSError as e:
        return (False, f"read-back failed: {e}")
    except ValueError as e:      # UnicodeDecodeError 等 = 壊れて書かれた
        return (False, f"read-back undecodable: {e}")


def _write_verified(path, data, *, binary: bool, encoding="utf-8", newline=None) -> None:
    """戦略を選んで書き、read-back で検証し、駄目ならリトライする唯一の実装。

    fragile FS では **rename を一度も呼ばない**（リトライ内でも replace へ退避しない）。
    すべての試行が失敗したら MountWriteError を送出する。黙って成功したことにはしない。
    """
    path = Path(path)
    strategy = _strategy_for(path)
    writer = _write_in_place if strategy == "inplace" else _write_via_replace

    # 内容が既に同一なら書かない。同期マウントでは 1 回の書込が upload・キャッシュ更新・
    # （replace 戦略では）rename を伴うので、no-op 書込の除去がそのまま障害機会の削減になる。
    # 実測では meta.json に同内容が何十回も書かれていた（キャッシュに孤児 tmp が 110 個）。
    already, _why = _read_back_matches(path, data, binary=binary,
                                       encoding=encoding, newline=newline)
    if already:
        return

    detail = "no attempt made"
    for attempt, delay in enumerate(_VERIFY_BACKOFF, start=1):
        if delay:
            time.sleep(delay)
        try:
            writer(path, data, binary=binary, encoding=encoding, newline=newline)
        except OSError as e:
            detail = f"write failed: {e}"
            continue
        ok, why = _read_back_matches(path, data, binary=binary,
                                     encoding=encoding, newline=newline)
        if ok:
            if attempt > 1:      # 一度失敗してから成功＝マウントが不安定。記録は残す
                _log.warning("mount write succeeded on attempt %d: %s", attempt, path)
            return
        detail = why
    payload = {"path": str(path), "attempts": len(_VERIFY_BACKOFF),
               "detail": detail, "strategy": strategy}
    _report_failure(payload)
    raise MountWriteError(path, attempts=len(_VERIFY_BACKOFF), detail=detail)


def atomic_write_text(
    path, text: str, *, encoding: str = "utf-8", newline: str | None = None
) -> None:
    """テキストを検証付きで書く（全書込の chokepoint）。

    fragile FS では in-place write、local FS では mkstemp + os.replace（モジュール docstring 参照）。
    どちらでも書込後に read-back で検証し、不一致なら最大 5 回リトライして、それでも駄目なら
    `MountWriteError` を送出する。

    改行変換は Path.write_text と同じ既定（newline=None: 書込時 '\\n' → os.linesep）に合わせるので、
    local FS での書き出しバイトは従来と byte-identical。親ディレクトリはここでは作らない
    （呼び出し側が従来どおり保証する）。
    """
    _write_verified(path, text, binary=False, encoding=encoding, newline=newline)


def atomic_write_bytes(path, data: bytes) -> None:
    """バイナリを検証付きで書く（`atomic_write_text` のバイナリ版）。

    PNG 等のバイナリ出力の chokepoint。matplotlib/PIL に直接パスを渡すと
    Image.save → os.path.realpath で WinError 1005 になるため（common.mount_compat 参照）、
    呼び出し側は BytesIO へ描画してから生バイトをここへ渡す。write は逐次バイト書き込みのみで
    realpath を経由しない。
    """
    _write_verified(path, data, binary=True)


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


def backup_text_if_changed(path, text: str) -> bool:
    """text を bak_path(path) に書く（既存 .bak と内容一致なら書かず False）。

    比較は read_text（universal newline）で行うので、atomic_write_text の
    newline 変換（Windows で '\\n'→os.linesep）による CRLF/LF 差は再書込を誘発しない。
    テキスト専用（durable_write_json は JSON 専用なので使わない）。

    戻り値: 書いたら True / 既存 .bak が一致してスキップしたら False。
    """
    bp = bak_path(path)
    try:
        if bp.read_text(encoding="utf-8") == text:
            return False
    except (OSError, ValueError):   # 不在 / 読めない / 非UTF-8 → (再)書込
        pass
    atomic_write_text(bp, text)
    return True


_SEQ_KEY = "_seq"   # durable_* が刻む単調カウンタ。呼び出し側からは見えない。


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return -1.0


def _seq_of(data: dict | None) -> int | None:
    """`_seq` を取り出す（無い/型違いは None = 旧形式）。"""
    if not isinstance(data, dict):
        return None
    seq = data.get(_SEQ_KEY)
    return seq if isinstance(seq, int) and not isinstance(seq, bool) else None


def _bak_is_newer(p: Path, pdata: dict, bp: Path, bdata: dict) -> bool:
    """.bak の方が新しいか（両方読めている前提）。同値なら False＝primary を採る。

    `_seq` が両方に有れば seq だけで決める（**mtime は見ない**: durable_write_json は
    primary → .bak の順に書くので、正常な書込でも .bak の mtime は常に primary 以上になり、
    mtime を混ぜると毎回 .bak が勝ってしまう）。
    片方にしか無ければ `_seq` を持つ方が新しい（新コードが書いたものだから）。
    どちらにも無い旧ファイル（#96 以前に書かれたもの）だけ mtime で決める — 同一の書込なら
    2 つの mtime はほぼ同時刻になるので、**primary が明確に古い＝primary の書込だけが失敗した**
    という #96 の症状をそのまま検出できる。
    """
    pseq, bseq = _seq_of(pdata), _seq_of(bdata)
    if pseq is not None or bseq is not None:
        return (bseq if bseq is not None else -1) > (pseq if pseq is not None else -1)
    return _mtime(bp) > _mtime(p) + 1.0    # 1 秒の猶予（同一書込の 2 コピーを別扱いしない）


def strip_seq(data: dict) -> dict:
    """`_seq` を取り除く。生の JSON を読んで内容比較する側が使う。

    `durable_write_json` が刻む `_seq` は durable_* の実装詳細で、`durable_read_json`
    は自動で剥がす。primary/.bak を `read_json_classified` で直接読んで内容比較する
    コード（no-op 判定・書込検証）は、比較の前にこれを通すこと。
    """
    return {k: v for k, v in data.items() if k != _SEQ_KEY}


def durable_write_json(path, data) -> None:
    """非再計算 JSON を primary＋サイドカー '.bak' の 2 コピーで検証付き書込。

    primary → .bak の順に書く。各コピーには単調な `_seq` を刻むので、片方の書込だけが
    失敗しても次回の `durable_read_json` が**新しい方**を選べる（Issue #96 以前は primary を
    無条件に優先していたため、書込に失敗した古い primary が良品の .bak を上書きし続けていた）。
    `_seq` は読取時に剥がされるので呼び出し側からは見えない。小さな JSON 前提。
    """
    p = Path(path)
    _pst, pdata = read_json_classified(p)
    _bst, bdata = read_json_classified(bak_path(p))
    prev = max(_seq_of(pdata) if _seq_of(pdata) is not None else -1,
               _seq_of(bdata) if _seq_of(bdata) is not None else -1, -1)   # 初回は 0 から
    payload = {**strip_seq(dict(data)), _SEQ_KEY: prev + 1}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    atomic_write_text(p, text)
    atomic_write_text(bak_path(p), text)


def durable_read_json(path, *, retries: int = 3, delay: float = 0.05):
    """primary と .bak の**両方**を読み、新しい方を返す純読取（**書込しない**）。never raise。

    返り値: ('ok', dict) / ('absent', None) / ('recovered', dict) / ('unreadable', None)。
    'ok' = primary が採用された / 'recovered' = .bak の方が新しかった（または primary が死んでいた）。

    Issue #96 以前は「primary が読めれば .bak を見ない」だったため、primary の書込だけが
    サイレントに失敗する（＝この同期マウントの主症状）と**常に古い方を読み、次の書込で
    良品の .bak を潰す**という破壊ループになっていた。両方読んで新しい方を採るのが修正。

    read 中に一切書かないので、非ロックな read でのレースは起きない。自己修復は呼び出し側の
    次回 durable_write_json（その exclusive_lock 内）が担う。
    """
    p, bp = Path(path), bak_path(path)
    status, data = read_json_classified(p, retries=retries, delay=delay)
    bstatus, bdata = read_json_classified(bp, retries=retries, delay=delay)
    pok, bok = status == "ok", bstatus == "ok"
    if pok and bok:
        # 両方読める → 新しい方を採る（同値なら primary＝従来挙動）
        if _bak_is_newer(p, data, bp, bdata):
            return ("recovered", strip_seq(bdata))
        return ("ok", strip_seq(data))
    if pok:
        return ("ok", strip_seq(data))
    if bok:
        return ("recovered", strip_seq(bdata))   # primary が失われても .bak が生存
    if status == "absent" and bstatus == "absent":
        return ("absent", None)       # 本当に両方無い → 新規作成/上書き安全
    return ("unreadable", None)       # どちらかは在るが読めない → 上書き禁止
