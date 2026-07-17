"""rclone/WinFsp マウント互換のためのプロセス全体 shim（WinError 1005 対策）。

根本原因は `common.paths.safe_resolve`（Issue #68）と同族。PIL の `Image.open` /
`Image.save` は path/str を渡されると内部で `os.path.realpath(path)` を呼び、
matplotlib の PNG 書き出しも PIL 経由なので `fig.savefig(path)` / `mpimg.imread(path)`
がいずれも `os.path.realpath` に到達する。Windows ではこれが `GetFinalPathNameByHandle`
を叩くが、WinFsp/rclone は未実装のため `OSError [WinError 1005]` を投げる。
`open()` / `write_text` / pandas は realpath を呼ばないので動く——これが差の正体。

`safe_resolve()` は「自分たちの呼び出し箇所」しか直せず、PIL/matplotlib の内部や
エージェント直書きコード（`plt.savefig("G:\\...")` / `mpimg.imread("G:\\...")`）が呼ぶ
realpath には届かない。`install()` は stdlib の `os.path.realpath` 自体を、同じ
「OSError → os.path.abspath（純 lexical 正規化）」フォールバックで包むことでその隙間を
塞ぐ。呼び出し側を一切いじらず、ライブラリ内部でも透過的にマウント上で動く。

影響範囲（安全性の要）: 挙動が変わるのは realpath が **今クラッシュしている箇所だけ**
（＝マウント上）。通常 FS では `realpath(strict=False)` は raise しないので結果は従来と
byte-identical。`strict=True` はそのまま forward し、その OSError（`FileNotFoundError`
等）は握り潰さず re-raise するので strict 契約は全呼び出し元で保たれる。ここが
`safe_resolve`（広い except OSError・自分のデータ経路限定なので可）より一段慎重な理由。

既知の限界: このパッケージも `install()` を呼ぶモジュール（config / core.figures /
common.image_io / common.explore）も import しない“素の”スクリプトには shim が入らず、
そのフローは依然 1005 になる。
"""
import os


def install() -> None:
    """`os.path.realpath` を OSError→abspath フォールバックで冪等にラップする。

    冪等判定は module フラグではなく **ラッパ関数の marker 属性** で行う。devtools の
    ホットリロードが本モジュールを purge/再 import しても、ラッパを保持している stdlib 側の
    `os.path.realpath` は purge されないため marker が生き残り、2 回目の install() は no-op に
    なる（module フラグだと再 import でリセットされ二重ラップし得る）。
    """
    orig = os.path.realpath
    if getattr(orig, "_mount_compat_shim", False):
        return

    def realpath(path, *, strict=False):
        try:
            return orig(path, strict=strict)
        except OSError:
            if strict:
                raise  # strict 契約を尊重（FileNotFoundError 等はそのまま）
            # WinFsp/rclone は realpath に答えられない（WinError 1005）。純 lexical な
            # 絶対パスへ退避する——common.paths.safe_resolve と同じポリシー。
            return os.path.abspath(path)

    realpath._mount_compat_shim = True
    os.path.realpath = realpath
