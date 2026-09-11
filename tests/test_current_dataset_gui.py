"""GUI tests for window-level current dataset (offscreen Qt)."""
import pytest


@pytest.fixture
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_dataset_tab_switch_updates_current(qapp):
    """dataset 付きタブ切替で current_dataset 更新 + dataset_changed 発火。"""
    from gui.window import ToolWindow
    from gui.tab import AnalysisTab

    win = ToolWindow()
    win.show()

    tab_a = AnalysisTab("a")
    tab_a.session_spec = {"kind": "analysis", "name": "a", "dataset": "ds_a"}
    win.add_tab(tab_a)

    tab_b = AnalysisTab("b")
    tab_b.session_spec = {"kind": "analysis", "name": "b", "dataset": "ds_b"}
    win.add_tab(tab_b)

    signals = []
    win.dataset_changed.connect(lambda ds: signals.append(ds))

    win.set_active_tab("a")
    assert win.current_dataset == "ds_a"

    win.set_active_tab("b")
    assert win.current_dataset == "ds_b"
    assert "ds_b" in signals


def test_datasetless_tab_switches_current_to_none(qapp):
    """明示選択モデル: dataset 無しタブ（None グループ）へ切替えると current は None。

    旧 sticky（追従保持）は廃止。current_dataset は「明示的に選択された
    データセット」で、None グループを前面化すれば None になる。"""
    from gui.window import ToolWindow
    from gui.tab import AnalysisTab

    win = ToolWindow()
    win.show()

    tab_ds = AnalysisTab("ds_tab")
    tab_ds.session_spec = {"kind": "analysis", "name": "ds_tab", "dataset": "ds_x"}
    win.add_tab(tab_ds)

    tab_plain = AnalysisTab("plain")
    tab_plain.session_spec = None
    win.add_tab(tab_plain)   # → None グループ（実 ds_x グループと共存）

    win.set_active_tab("ds_tab")
    assert win.current_dataset == "ds_x"

    win.set_active_tab("plain")
    assert win.current_dataset is None  # 明示選択（sticky ではない）


def test_set_active_tab_selects_dataset(qapp):
    """figure タブへの set_active_tab がその dataset グループを前面化し current を更新。"""
    from gui.window import ToolWindow
    from gui.tab import AnalysisTab

    win = ToolWindow()
    win.show()

    tab = AnalysisTab("t")
    tab.session_spec = {"kind": "figure", "name": "t", "dataset": "ds_y", "figure": "/tmp/x.png"}
    win.add_tab(tab)

    win.set_active_tab("t")
    assert win.current_dataset == "ds_y"
    win.notify_chat_dataset()  # チャット push のみ、current は変えない
    assert win.current_dataset == "ds_y"


# --------------------------------------------------------------------------- #
# File → データセットを新規登録 — 実ストレージ（tmp の JSON）を通す（Issue #95）
# --------------------------------------------------------------------------- #
@pytest.fixture
def register_stubs(monkeypatch, tmp_path):
    """入力ダイアログ・ディレクトリ選択・完了通知を stub 化して返す。"""
    import json

    import config
    import dataset_registry
    from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

    reg = tmp_path / "datasets.local.json"
    monkeypatch.setattr(dataset_registry, "registry_path", lambda: reg)

    ds_dir = tmp_path / "sample_dataset"
    ds_dir.mkdir()

    shown = {"info": 0, "critical": 0}
    monkeypatch.setattr(
        QInputDialog, "getText",
        staticmethod(lambda *a, **k: ("sample_dataset", True)),
    )
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory",
        staticmethod(lambda *a, **k: str(ds_dir)),
    )
    monkeypatch.setattr(
        QMessageBox, "information",
        staticmethod(lambda *a, **k: shown.__setitem__("info", shown["info"] + 1)),
    )
    monkeypatch.setattr(
        QMessageBox, "critical",
        staticmethod(lambda *a, **k: shown.__setitem__("critical", shown["critical"] + 1)),
    )

    class S:
        pass

    s = S()
    s.reg = reg
    s.ds_dir = ds_dir
    s.shown = shown
    s.read = lambda: json.loads(reg.read_text(encoding="utf-8-sig"))
    s.config = config
    return s


def test_gui_register_writes_json_and_memory(qapp, register_stubs):
    from gui.window import ToolWindow

    win = ToolWindow()
    win._register_dataset()

    assert register_stubs.read() == {
        "sample_dataset": {
            __import__("socket").gethostname().upper(): str(register_stubs.ds_dir)
        }
    }
    assert "sample_dataset" in register_stubs.config.DATASETS
    assert win.current_dataset == "sample_dataset"
    assert register_stubs.shown["info"] == 1
    assert register_stubs.shown["critical"] == 0


def test_gui_register_failure_updates_nothing(qapp, register_stubs, monkeypatch):
    import config
    from gui.window import ToolWindow

    def boom(*a, **k):
        raise config.RegistryError("nope")

    monkeypatch.setattr(config, "register_dataset", boom)

    win = ToolWindow()
    before = dict(config.DATASETS)
    before_current = win.current_dataset
    win._register_dataset()

    assert config.DATASETS == before
    assert win.current_dataset == before_current
    assert not register_stubs.reg.exists()
    assert register_stubs.shown["critical"] == 1
    assert register_stubs.shown["info"] == 0
