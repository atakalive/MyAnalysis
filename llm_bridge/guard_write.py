"""PreToolUse hook: エージェントの Write/Edit が同期マウントを直接叩くのを止める（Issue #96）。

## なぜ必要か

我々のコードの書込は `common.paths` の chokepoint を通るので、fragile FS では
rename-into-place を避け read-back 検証もされる。しかし**チャットエージェント自身の
Write/Edit ツールはそこを通らない** — 独自に `<name>.tmp.<pid>.<hex>` を作って
rename するので、rclone のキャッシュ層で 22% の確率で失敗し、対象ファイルが
0 バイト化する。実際に `_work/code/*.py` が 0 バイトになり、中身はキャッシュの
孤児 tmp からしか回収できなかった。

プロンプト（`llm_backend/base.py` の MOUNT_SAFE_EDITS）で「draft→apply を使え」と
指示していたが、実測で守られていなかった。したがって機械的に拒否する。

## 契約

stdin に Claude Code の hook JSON、stdout に決定 JSON、終了コードは常に 0。
**内部エラーは必ず fail-open**（許可）にする — ガードのバグでエージェントが
完全に停止する方が、たまに 0 バイト化するより有害なため。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# ツール名 → 書込先を持つ入力キー
_PATH_KEYS = ("file_path", "notebook_path", "path")


def _deny(reason: str) -> int:
    # ensure_ascii=True（既定）は必須。ensure_ascii=False だと非 ASCII が stdout の
    # ロケール既定エンコーディングで出るため、日本語 Windows では cp932 バイト列になり、
    # UTF-8 で読む消費側が JSON をパースできず **deny が失われて fail-open する**。
    # 案内文を英語にしても救われない —— 拒否理由には対象パスを埋め込むので、
    # G:\測定\... のような日本語パスだけで同じ事故になる。
    # ASCII エスケープ（\uXXXX）にしておけばどのエンコーディングでも同一に読める。
    json.dump({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}, sys.stdout)
    sys.stdout.write("\n")
    return 0


def _allow() -> int:
    return 0


def _guidance(target: Path) -> str:
    """拒否理由 — 代わりに何をすればよいかを具体的に書く。"""
    parts = [
        f"{target} は同期マウント（rclone）上にあります。",
        "このマウントでは rename-into-place が約 22% の確率で失敗し、"
        "書込 API が成功を返したままファイルが 0 バイトになります"
        "（Write/Edit ツールは内部で tmp+rename を使うため、この経路に乗ります）。",
        "",
        "代わりに次のマウント安全な経路を使ってください:",
    ]
    if target.name == "analysis.py" and target.parent.parent.name == "analyses":
        name = target.parent.name
        parts += [
            f"  python -m llm_bridge draft-analysis {name} --dataset <ds>",
            "     → work_dir 上の draft を編集（draft は Write/Edit してよい）",
            f"  python -m llm_bridge apply-analysis {name} --dataset <ds>",
            "     → 構文検証のうえ検証付きで analysis.py へ昇格",
        ]
    else:
        parts += [
            "  python -c \"from common.explore import save_text; "
            "save_text('<dataset>', '<relpath>', content)\"",
            "     → work_dir へ任意の拡張子・サブディレクトリで検証付き書込",
            "       （例: 'summary.md', 'reports/2026-07.csv'）",
            "  python -c \"from common.explore import save_code; "
            "save_code('<dataset>', '<label>', content)\"",
            "     → work_dir/code/<label>.py（.py 専用。label に拡張子は付けない）",
            "  図は save_fig('<dataset>', fig, '<label>') を使う。",
            "  いずれも保存先の絶対パスを返すので print() すること。",
        ]
    parts += ["", "状態を確認するには: python -m llm_bridge doctor"]
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return _allow()
        event = json.loads(raw)
        tool_input = event.get("tool_input") or {}
        target = None
        for key in _PATH_KEYS:
            val = tool_input.get(key)
            if isinstance(val, str) and val:
                target = Path(val)
                break
        if target is None:
            return _allow()

        # draft は編集させる。MOUNT_SAFE_EDITS と下の _guidance が「draft は Write/Edit
        # してよい」と案内している一方で、ここが fragile な宛先を無条件に拒否していたため
        # 宣伝している draft→apply ループが機械的に成立していなかった。許可して安全なのは、
        # apply_analysis が strip / ast.parse / build_tab 束縛の 3 ゲートを通してからしか
        # 昇格させないので、draft が 0 バイト化しても analysis.py に伝播しないため
        # （draft は使い捨てで、壊れたら draft-analysis で作り直せる）。
        if target.name == "analysis.draft.py":
            return _allow()

        from common import fs_kind
        if not fs_kind.is_fragile(target):
            return _allow()
        return _deny(_guidance(target))
    except Exception:                       # noqa: BLE001 — ガードは絶対に落とさない
        # fail-open。診断のため stderr にだけ残す（stdout は決定 JSON 専用）。
        import traceback
        traceback.print_exc(file=sys.stderr)
        return _allow()


if __name__ == "__main__":
    raise SystemExit(main())
