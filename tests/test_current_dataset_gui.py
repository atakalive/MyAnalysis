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
    calls: list[str] = []
    defaults: list[str] = []
    # 名前入力への返答。空なら既定値（= フォルダ名）をそのまま受け入れる。
    answers: list[tuple[str, bool]] = []

    def get_text(*a, **k):
        calls.append("name")
        defaults.append(k.get("text", ""))
        return answers.pop(0) if answers else (k.get("text", ""), True)

    def get_dir(*a, **k):
        calls.append("dir")
        return str(ds_dir)

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(get_text))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(get_dir))
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
    s.calls = calls
    s.defaults = defaults
    s.answers = answers
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
    # フォルダ選択が先、名前の既定値はフォルダ名
    assert register_stubs.calls == ["dir", "name"]
    assert register_stubs.defaults == ["sample_dataset"]


def test_gui_register_dir_cancel_asks_nothing(qapp, register_stubs, monkeypatch):
    import config
    from PySide6.QtWidgets import QFileDialog
    from gui.window import ToolWindow

    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: "")
    )

    win = ToolWindow()
    before = dict(config.DATASETS)
    win._register_dataset()

    assert register_stubs.calls == []  # 名前入力は出ない
    assert config.DATASETS == before
    assert not register_stubs.reg.exists()


def test_gui_register_invalid_name_reprompts(qapp, register_stubs):
    from gui.window import ToolWindow

    register_stubs.answers[:] = [("bad name", True), ("good_name", True)]

    win = ToolWindow()
    win._register_dataset()

    # 無効名はエラー表示後、フォルダを選び直させず入力値を残して再入力
    assert register_stubs.calls == ["dir", "name", "name"]
    assert register_stubs.defaults == ["sample_dataset", "bad name"]
    assert register_stubs.shown["critical"] == 1
    assert register_stubs.shown["info"] == 1
    assert list(register_stubs.read()) == ["good_name"]
    assert win.current_dataset == "good_name"


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
