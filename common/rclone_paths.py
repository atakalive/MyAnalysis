"""rclone のキャッシュ・ログの場所の解決（doctor と devtools.mount_probe で共有）。

既定値は持たない。コマンドの引数、無ければ環境変数、どちらも無ければ None（＝未指定）。
存在確認はしない（「未指定」と「指定したが見つからない」は呼び出し側で区別して表示する）。
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_CACHE = "MYANALYSIS_RCLONE_CACHE"
ENV_LOG = "MYANALYSIS_RCLONE_LOG"


def resolve(arg: str | os.PathLike[str] | None, env_name: str) -> Path | None:
    """引数 > 環境変数 ``env_name`` > None。

    空・空白だけ・"."（``Path("")`` の文字列形）は未指定として扱い、次の候補を見る。
    """
    for raw in (arg, os.environ.get(env_name)):
        if raw is None:
            continue
        value = os.fspath(raw).strip()
        if value and value != ".":
            return Path(value)
    return None
