import argparse
import sys
from collections.abc import Callable

from PySide6.QtWidgets import QApplication, QLabel

import llm_bridge
from common.env import load_env
from gui import apply_dark_theme
from gui.chat import ChatWidget
from llm_backend import get_backend
from gui.tab import AnalysisTab
from gui.tools import make_dispatch
from gui.window import ToolWindow


def build_demo_tab() -> tuple[
    AnalysisTab, Callable[[], dict], Callable[[dict], None]
]:
    """合成データで全パネル種別を動かす検証用タブ。"""
    import numpy as np

    from gui.panels import ImagePanel, SelectorPanel, TrajectoryPanel

    tab = AnalysisTab(name="_demo")

    units = ["unit_A", "unit_B", "unit_C"]
    rng = np.random.default_rng(0)
    unit_data = {}
    for u in units:
        offset = 5 * rng.standard_normal()
        unit_data[u] = {
            "x": np.arange(60),
            "y": np.cumsum(rng.standard_normal(60)) - offset,
            "phase": ["warmup"] * 10 + ["scan"] * 30 + ["refine"] * 20,
            "images": [rng.standard_normal((100, 200)) for _ in range(60)],
        }
    state = {"unit": units[0], "iter": 0}

    sel = SelectorPanel("Unit", units)
    traj = TrajectoryPanel()
    img = ImagePanel()
    tab.add_panel("sel", sel, "top")
    tab.add_panel("traj", traj, "left")
    tab.add_panel("img", img, "right")

    def render() -> None:
        d = unit_data[state["unit"]]
        traj.set_data(d["x"], d["y"], color_by=d["phase"])
        img.set_image(d["images"][state["iter"]])

    def on_unit(u: str) -> None:
        state["unit"] = u
        state["iter"] = 0
        render()
        tab.dispatch_command("refresh-state")

    def on_point(i: int) -> None:
        state["iter"] = i
        img.set_image(unit_data[state["unit"]]["images"][i])
        tab.dispatch_command("refresh-state")

    sel.selectionChanged.connect(on_unit)
    traj.pointClicked.connect(on_point)
    render()

    def state_provider() -> dict:
        return {"unit": state["unit"], "iter": state["iter"]}

    def annotations_handler(ann: dict) -> None:
        traj.clear_markers()
        img.clear_notes()
        for m in ann.get("markers", []):
            traj.add_marker(
                float(m.get("x", 0)),
                color=str(m.get("color", "#ff0000")),
                label=str(m.get("label", "")),
            )
        for n in ann.get("notes", []):
            img.add_note(
                str(n.get("text", "")),
                x=float(n.get("x", 0)),
                y=float(n.get("y", 0)),
            )

    return tab, state_provider, annotations_handler


def build_placeholder_tab() -> tuple[
    AnalysisTab, Callable[[], dict] | None, Callable[[dict], None] | None
]:
    tab = AnalysisTab(name="(empty)")
    tab.add_panel(
        "msg",
        QLabel("No analyses defined yet. Try: python tool.py --demo"),
        "top",
    )
    return tab, None, None


def main() -> None:
    load_env()

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--demo", action="store_true", help="Launch with a synthetic demo tab"
    )
    args = parser.parse_args()

    app = QApplication(sys.argv)
    apply_dark_theme(app)
    win = ToolWindow()
    tab, state_provider, ann_handler = (
        build_demo_tab() if args.demo else build_placeholder_tab()
    )
    win.add_tab(tab)

    win.set_chat_widget(ChatWidget(get_backend(), make_dispatch(win)))

    _window_watchers = llm_bridge.attach_window(win)
    # connect_annotations must run before attach_tab: attach_tab's annotations
    # watcher calls tab.apply_annotations(read(...)) initially, which no-ops if
    # the handler is not yet connected.
    if ann_handler is not None:
        tab.connect_annotations(ann_handler)
    _tab_watchers = (
        llm_bridge.attach_tab(tab, state_provider) if state_provider is not None else []
    )

    if state_provider is not None:
        tab.dispatch_command("refresh-state")

    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
