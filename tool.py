import argparse
import sys

from PySide6.QtWidgets import QApplication, QLabel

from gui import apply_dark_theme
from gui.tab import AnalysisTab
from gui.window import ToolWindow


def build_demo_tab() -> AnalysisTab:
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

    def on_point(i: int) -> None:
        state["iter"] = i
        img.set_image(unit_data[state["unit"]]["images"][i])

    sel.selectionChanged.connect(on_unit)
    traj.pointClicked.connect(on_point)
    render()
    return tab


def build_placeholder_tab() -> AnalysisTab:
    tab = AnalysisTab(name="(empty)")
    tab.add_panel(
        "msg",
        QLabel("No analyses defined yet. Try: python tool.py --demo"),
        "top",
    )
    return tab


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true",
                        help="Launch with a synthetic demo tab")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    apply_dark_theme(app)
    win = ToolWindow()
    win.add_tab(build_demo_tab() if args.demo else build_placeholder_tab())
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
