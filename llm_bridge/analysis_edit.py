"""Mount-safe edit path for existing analyses: draft → apply → recover.

Headless CLI verbs (no PySide6). Path resolution + containment come from
dataset_config; atomic writes from common.paths. All errors raise
SystemExit("error: ..."); success prints to stdout.

Validation in apply is syntax (ast.parse) + a static check that the name
`build_tab` is bound at module top level (any common form: def / async def /
assignment / annotated assignment / import). This approximates the runtime
contract (hasattr(mod, "build_tab") after exec) WITHOUT executing the draft,
so it does not reject valid analyses that bind build_tab via import or
assignment. It does NOT verify build_tab is callable or runtime-error-free,
and dynamic bindings (exec / globals() / conditional) are not recognized. A
non-empty, syntactically valid but semantically broken analysis can still be
promoted; the .bak (last good build) and git/chat history are the backstop.
"""
import ast
import sys
import time

import dataset_config
from common.paths import atomic_write_text, bak_path

_BUILD_TAB = "build_tab"


def _target(dataset: str, name: str):
    """Resolve the analysis.py path (containment-checked), or SystemExit."""
    try:
        return dataset_config.analysis_file(dataset, name)
    except (ValueError, KeyError, RuntimeError) as e:
        raise SystemExit(f"error: {e}")


def _defines_build_tab(tree: ast.Module) -> bool:
    """True if the module top level binds the name ``build_tab`` by any common
    static form: def / async def / assignment / annotated assignment / import.

    Approximates _build_analysis's runtime contract (hasattr(mod, 'build_tab')
    after exec) without executing the draft, so import/assignment definitions
    are accepted (not falsely rejected). Dynamic bindings (exec, globals(),
    conditional) are NOT recognized — build_tab must be a plain top-level stmt.
    """
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == _BUILD_TAB:
                return True
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                names = tgt.elts if isinstance(tgt, (ast.Tuple, ast.List)) else [tgt]
                if any(isinstance(n, ast.Name) and n.id == _BUILD_TAB for n in names):
                    return True
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == _BUILD_TAB:
                return True
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound == _BUILD_TAB:
                    return True
    return False


def draft_analysis(dataset: str, name: str) -> None:
    af = _target(dataset, name)
    if not af.is_file():
        raise SystemExit(
            f"error: no analysis named {name!r} under {dataset!r}; "
            f"create it with: python -m newanalysis {name} --dataset {dataset}"
        )
    try:
        source = af.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        raise SystemExit(
            f"error: cannot read {af} ({e}); if it is empty/corrupt, run: "
            f"python -m llm_bridge recover-analysis {name} --dataset {dataset}"
        )
    if not source:   # byte-exact: 真の 0 バイトだけを recover 案内にする（統一規約）
        raise SystemExit(
            f"error: {af} is empty (0 bytes); recover it first: "
            f"python -m llm_bridge recover-analysis {name} --dataset {dataset}"
        )
    out_dir = dataset_config.analysis_out_dir(dataset, name, create=True)
    draft = out_dir / "analysis.draft.py"
    atomic_write_text(draft, source)
    print(str(draft))
    # reload は dataset を受け取らず active dataset のタブを対象にするので、
    # 同名衝突を避けるため set-active-dataset を前置する（reviewer R2 P2）。
    print(
        f"edit the file above, then promote it: "
        f"python -m llm_bridge apply-analysis {name} --dataset {dataset} && "
        f"python -m llm_bridge window set-active-dataset name={dataset} --wait && "
        f"python -m llm_bridge window reload scope=tab target={name} --wait"
    )


def apply_analysis(dataset: str, name: str) -> None:
    af = _target(dataset, name)
    try:
        out_dir = dataset_config.analysis_out_dir(dataset, name, create=False)
    except (ValueError, KeyError, RuntimeError) as e:
        raise SystemExit(f"error: {e}")
    draft = out_dir / "analysis.draft.py"
    if not draft.is_file():
        raise SystemExit(
            f"error: no draft for {name!r}; run: "
            f"python -m llm_bridge draft-analysis {name} --dataset {dataset}"
        )
    # read_text（universal newline）で読む＝改行を '\n' に正規化してから
    # atomic_write_text（newline=None）で書き直すので二重CRにならない。
    # 空・非UTF-8・一時的な read 不能は 0.05s ×3 リトライで VFS 同期ラグを吸収し、
    # リトライアウトしたときだけエラーにする（reviewer/reviewer P2）。
    source = None
    for attempt in range(3):
        try:
            candidate = draft.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):   # 部分同期/破損は一過性かも → リトライ
            candidate = ""
        if candidate.strip():   # ゲートは strip（空白のみの draft は昇格させない）
            source = candidate
            break
        if attempt < 2:
            time.sleep(0.05)
    if source is None:
        raise SystemExit(
            "error: draft read failed after retries "
            "(empty, sync lag, or unreadable); re-edit and retry"
        )
    # 構文検証 + build_tab 束縛検証を AST で行う（部分文字列検索の誤検出を避け、
    # かつ def 以外の束縛形も認識して実ランタイム契約に近づける）。
    try:
        tree = ast.parse(source, str(af))
    except SyntaxError as e:
        raise SystemExit(f"error: syntax {e.filename}:{e.lineno}: {e.msg}")
    if not _defines_build_tab(tree):   # 必須の build_tab 束縛不在は fail-fast（reviewer P1）
        raise SystemExit(
            "error: draft does not bind a top-level `build_tab` (checked def / "
            "async def / assignment / import forms); an analysis module must "
            "define build_tab at module level (not applied). Dynamic bindings "
            "(exec / globals / conditional) are not recognized."
        )
    af.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(af, source)   # 昇格。draft は残す（次の反復は Edit → apply）。
    print(f"applied: {af}")
    # reload は active dataset のタブが対象なので set-active-dataset を前置（reviewer R2 P2）。
    print(
        f"reload:  python -m llm_bridge window set-active-dataset name={dataset} "
        f"--wait && python -m llm_bridge window reload scope=tab target={name} "
        f"--wait"
    )


def recover_analysis(dataset: str, name: str) -> None:
    af = _target(dataset, name)
    try:
        current = af.read_text(encoding="utf-8")
    except FileNotFoundError:                 # 不在 → 復旧対象
        current = ""
    except (UnicodeDecodeError, OSError) as e:  # 在るが読めない → 触らない
        raise SystemExit(
            f"error: {af} is present but unreadable ({e}); it may be mid-sync — "
            f"retry later; not overwriting"
        )
    if current:   # byte-exact: 1 バイトでも中身（空白含む）があれば上書きしない（reviewer P1）
        raise SystemExit(
            f"error: {af} is not empty; refusing to overwrite (recover only "
            f"restores a 0-byte or absent analysis.py)"
        )
    bp = bak_path(af)
    try:
        bak_text = bp.read_text(encoding="utf-8")
    except (OSError, ValueError):
        raise SystemExit(
            f"error: no usable backup at {bp}; the .bak lives on the same synced "
            f"mount, so a drive-level loss is not covered — restore from git or "
            f"chat history"
        )
    if not bak_text:   # 0 バイトの .bak は復旧に使えない
        raise SystemExit(f"error: backup {bp} is empty; cannot recover")
    af.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(af, bak_text)
    # stale draft を消して draft↔analysis.py の乖離を断つ（reviewer P2 経路 a）:
    # 消さないと、回復済み analysis.py を古い draft で apply して再破棄する事故が起きる。
    # 削除は成功/未存在/失敗を区別して出力（"removed" 固定表示は安全状態の誤認を招く=reviewer R2 P2）。
    draft_note = None
    try:
        stale = dataset_config.analysis_out_dir(dataset, name, create=False) \
            / "analysis.draft.py"
    except (OSError, ValueError, KeyError, RuntimeError):
        stale = None
    if stale is not None:
        if stale.exists():
            try:
                stale.unlink()
                draft_note = "stale draft removed"
            except OSError:
                draft_note = (
                    f"WARNING: could not remove stale draft {stale} — "
                    f"delete it manually before editing again"
                )
        else:
            draft_note = "no stale draft to remove"
    print(f"recovered: {af} (from {bp} — last successfully-built content).")
    if draft_note:
        print(draft_note)
    print(
        f"before editing again run: "
        f"python -m llm_bridge draft-analysis {name} --dataset {dataset}"
    )
