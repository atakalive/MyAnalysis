"""Cross-platform subprocess helpers."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
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
