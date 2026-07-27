"""Cross-platform subprocess helpers."""
from __future__ import annotations

import subprocess
import sys


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
