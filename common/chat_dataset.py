"""チャットのデータセットをエージェントへ渡すための共有定義（Issue #111）。

gui/chat.py・gui/tools.py・llm_backend/・llm_bridge/__main__.py・llm_bridge/commands.py が
同じ名前と同じ検査を使う。Qt にも他のプロジェクトモジュールにも依存しない。
"""
from __future__ import annotations

# GUI がエージェントの子プロセス（claude / codex / pi）に設定し、`python -m llm_bridge` が
# dataset 省略時の既定に使う。内部用（.env に書かない）。
CHAT_DATASET_ENV = "MYANALYSIS_CHAT_DATASET"


def chat_dataset_value(value: object) -> str | None:
    """value が DS 名として使える（空でない str で NUL を含まない）ならそのまま返し、
    そうでなければ None を返す。

    sess.dataset は同期ファイル由来で型が保証されない（session_from_dict は検査しない）。
    NUL は環境変数に入れられない（Popen が ValueError を送出する）ので弾く。"""
    if isinstance(value, str) and value and "\x00" not in value:
        return value
    return None
