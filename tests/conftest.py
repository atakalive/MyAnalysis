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

ui_prefs.json（言語・tool_display・全体ペルソナ等の UI 設定）と personas.json
（ペルソナ定義ストア）も同じ層: update_ui_pref / personas の save 経路を素通し
すると、スイートを走らせるたびに開発者の実設定・実定義が書き換わる。
global_state_dir 経由で ui_prefs を解決するテストは、同一属性 ui_prefs_path を
自前で patch し直すこと（後勝ちで autouse に勝つ）。

dataset_registry の既定パス（`<repo>/datasets.local.json`）も同様。config は import 時
（module 末尾の reload_datasets()）に登録簿を読むので、autouse fixture では collection 中の
読み込みを隔離できない。**このモジュールのロード時**に `dataset_registry.registry_path` を
一時領域の未作成 JSON へ差し替え、どのテストモジュールが `config` を import するより前に
隔離を成立させる（差し替え先は使用側モジュール `dataset_registry` — `config.registry_path`
は再公開の別名で read/transaction の本体が見る名前ではない）。
"""

import tempfile
from pathlib import Path

import pytest

import dataset_registry as _dataset_registry

# --- collection 前に既定登録簿を一時領域へ向ける（実 datasets.local.json を触らない） ---
_REG_TMP = tempfile.TemporaryDirectory(prefix="myanalysis-registry-")
_ORIG_REGISTRY_PATH = _dataset_registry.registry_path
_dataset_registry.registry_path = lambda: Path(_REG_TMP.name) / "datasets.local.json"


def pytest_unconfigure(config):
    _dataset_registry.registry_path = _ORIG_REGISTRY_PATH
    _REG_TMP.cleanup()


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
    monkeypatch.setattr(
        lb_paths, "ui_prefs_path", lambda: tmp_path / "ui_prefs.json"
    )
    monkeypatch.setattr(
        lb_paths, "personas_path", lambda: tmp_path / "personas.json"
    )


@pytest.fixture(autouse=True)
def _isolate_dataset_registry(monkeypatch, tmp_path):
    """各テストの既定登録簿を tmp_path 配下の未作成 JSON へ向け、DATASETS を空にする。

    ロック実体の退避先（`repo_root()/data/locks/`）へ漏らさないため、FS 判定を
    tmp_path=local に固定する（`MYANALYSIS_FORCE_FRAGILE` / `MYANALYSIS_WRITE_STRATEGY` は
    継承させない）。`common.paths.repo_root` はここでは差し替えない — i18n カタログの
    lazy ロードが `repo_root()/i18n` を見るため。
    """
    import config
    import config_share
    import dataset_registry

    monkeypatch.delenv("MYANALYSIS_FORCE_FRAGILE", raising=False)
    monkeypatch.delenv("MYANALYSIS_WRITE_STRATEGY", raising=False)
    monkeypatch.setenv("MYANALYSIS_FS_OVERRIDE", f"{tmp_path}=local")
    from common import fs_kind
    fs_kind.cache_clear()

    # 同期（config_share）の実接続・実状態ファイル・実 .env を遮断する（設計 §6.1）。
    # register-dataset CLI 等は main の try_sync まで到達するため、共通 autouse で塞ぐ。
    # 同期専用の env fixture のみが後勝ちで FakeS3 等へ上書きする。
    monkeypatch.setenv("R2_AUTOSYNC", "0")
    monkeypatch.setattr(config_share, "load_env", lambda *a, **k: None)
    monkeypatch.setattr(
        config_share, "_state_path",
        lambda: tmp_path / "config_share_state.json",
    )

    def _no_client(_creds):
        raise RuntimeError("network disabled in tests (config_share._client stub)")

    monkeypatch.setattr(config_share, "_client", _no_client)

    monkeypatch.setattr(
        dataset_registry, "registry_path",
        lambda: tmp_path / "datasets.local.json",
    )
    # in-place で空にする（再代入すると `from config import DATASETS` の保持者が
    # 古い dict を見続け、reload の反映契約がテスト内だけ壊れる）。
    saved = {k: dict(v) for k, v in config.DATASETS.items()}
    config.DATASETS.clear()
    try:
        yield
    finally:
        config.DATASETS.clear()
        config.DATASETS.update(saved)
        fs_kind.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_session_ownership(monkeypatch):
    """session の持ち主・チャット baseline の記録をテストごとに空にする（Issue #106）。

    テスト間で持ち越すと結果が順序依存になる。関数はモジュールグローバルを実行時に
    引くので、新しい dict への差し替えが効く。
    """
    from llm_bridge import session
    for name in ("_restored", "_unreadable", "_chat_baseline", "_last_skipped"):
        monkeypatch.setattr(session, name, {})
