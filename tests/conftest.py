"""Shared test isolation: keep the suite from writing repo-real PC-local state.

save_all のライドアロング write_last_window と open_dataset の
note_recent_dataset は data/llm_state/ の実ファイル（last_window.json /
recent_datasets.json）を書く。テストがこれを素通しすると、スイートを走らせる
たびに開発者の実ワークスペース記録（File → 前回のセッションを復元 の対象）と
picker の MRU が空/テスト用データセット名で破壊される。autouse で tmp へ
リダイレクトする（自前で patch するテストはその patch が後勝ちで有効）。

backend_sessions.json（ネイティブ resume token の PC ローカルストア）も同様。
_capture_backend_session がターン毎に書くので、隔離しないと開発者の実チャットの
token をテストが上書きするうえ、残留エントリで後続テストが順序依存になる。
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_pc_local_state(monkeypatch, tmp_path):
    from llm_bridge import paths as lb_paths
    monkeypatch.setattr(
        lb_paths, "last_window_path", lambda: tmp_path / "last_window.json"
    )
    monkeypatch.setattr(
        lb_paths, "recent_datasets_path",
        lambda: tmp_path / "recent_datasets.json",
    )
    monkeypatch.setattr(
        lb_paths, "backend_sessions_path",
        lambda: tmp_path / "backend_sessions.json",
    )
