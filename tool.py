import sys

if __name__ == "__main__":
    # 重い import より前に: 起動失敗を pythonw でも知らせ、import 以降の失敗は
    # data/logs/ に残す（版の確認はフックより前なので、版不足はログに残らない）。
    from common import crashlog

    crashlog.require_python((3, 11))  # 古い Python で動かすコードを最小にするため先頭
    crashlog.install()
    crashlog.install_file_logging()

try:
    import argparse
    from collections.abc import Callable

    from PySide6.QtWidgets import QApplication, QLabel

    import llm_bridge
    from common import crashlog
    from common.env import load_env
    from common.i18n import current_language, init_language, tr
    from gui import apply_dark_theme
    from gui.chat import ChatWidget
    from gui.qt_translation import install_qt_translator
    from llm_backend import get_backend
    from gui.tab import AnalysisTab
    from gui.tools import make_dispatch
    from gui.window import ToolWindow
except Exception as _e:
    if __name__ != "__main__":
        raise  # テスト・Tier 3 再構築の import tool は従来どおり
    crashlog.report_startup_failure(_e)
    sys.exit(1)


def build_demo_tab() -> tuple[
    AnalysisTab, Callable[[], dict], Callable[[dict], None]
]:
    """合成データで SelectorPanel / TrajectoryPanel / ImagePanel を動かす検証用タブ（FigurePanel は含まない）。"""
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
    tab = AnalysisTab(name="(empty)", is_placeholder=True)
    tab.add_panel(
        "msg",
        QLabel(tr("tab.placeholder.body")),
        "top",
    )
    return tab, None, None


def create_main_window(
    app: QApplication,
    *,
    demo: bool = False,
    watcher_resume_after: float | None = None,
    resume_session: bool = False,
) -> ToolWindow:
    """Build the ToolWindow, wire the bridge, and return it (not yet shown).

    Extracted from ``main()`` so the hot-reload Tier 3 (blue-green) path can
    rebuild a fresh window in-process. The command/tab watchers are kept alive
    on ``win._bridge_watchers`` (was a local in main(), which made teardown
    impossible).

    ``watcher_resume_after`` is passed transparently to ``attach_window`` →
    ``commands.start_watcher`` so commands queued after a rebuild epoch are not
    dropped as stale.

    ``resume_session`` requests consuming the reload manifest (Tier 3/4
    restore). When the manifest is absent / corrupt / missing required keys,
    falls back to ordinary startup (placeholder tab).
    """
    # Re-hydrate the active UI language from ui_prefs.json before building the
    # window. This runs on every window construction — not just process start —
    # so a Tier 3 (app / blue-green) rebuild restores it too: that path purges
    # common.i18n and re-imports it fresh (resetting _active to the "en" default)
    # without going through main(). Read-only/idempotent; safe to call each time.
    init_language()
    install_qt_translator(app, current_language())
    win = ToolWindow()
    tab, state_provider, ann_handler = (
        build_demo_tab() if demo else build_placeholder_tab()
    )
    win.add_tab(tab)

    win.set_chat_widget(ChatWidget(get_backend, make_dispatch(win)))

    watchers = list(
        llm_bridge.attach_window(win, watcher_resume_after=watcher_resume_after)
    )
    # connect_annotations must run before attach_tab: attach_tab's annotations
    # watcher calls tab.apply_annotations(read(...)) initially, which no-ops if
    # the handler is not yet connected.
    if ann_handler is not None:
        tab.connect_annotations(ann_handler)
    if state_provider is not None:
        watchers += llm_bridge.attach_tab(tab, state_provider)
        tab.dispatch_command("refresh-state")

    win._bridge_watchers = watchers

    from devtools.qt_integration import install_hotreload

    win._hotreload = install_hotreload(win)

    # Meeting relay (Issue #42): live-share the chat dock + analysis view to
    # remote guests. Created after the chat widget is mounted so it can connect
    # to ChatWidget.messageAdded. stop() is idempotent and wired on three
    # teardown paths: aboutToQuit (here, via shutdown() = stop() + close the
    # local relay server), ToolWindow.closeEvent, and the hot-reload Tier 3
    # rebuild (both use stop(), keeping the app-scoped local server alive).
    from meeting.relay import MeetingRelay

    win._meeting_relay = MeetingRelay(win)
    app.aboutToQuit.connect(win._meeting_relay.shutdown)

    if resume_session:
        from devtools.qt_integration import consume_manifest

        consume_manifest(win)

    return win


def main() -> None:
    crashlog.install()
    load_env()
    # 言語初期化は create_main_window() 側に一本化（Tier 3 再構築でも復元されるよう）。

    # 起動時の best-effort config 同期（short timeout で有界・繋がるときだけ）。
    # try_sync が内部で全例外を握りつぶすが、import 失敗等の二重防御で外側も握る。
    try:
        import config_share
        config_share.try_sync()
    except Exception:
        pass

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--demo", action="store_true", help="Launch with a synthetic demo tab"
    )
    parser.add_argument(
        "--resume-session",
        action="store_true",
        help="Restore window/tabs/chat from the reload manifest (Tier 4 restart)",
    )
    args = parser.parse_args()

    app = QApplication(sys.argv)
    apply_dark_theme(app)
    win = create_main_window(
        app, demo=args.demo, resume_session=args.resume_session
    )
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    try:
        main()
    except Exception as _e:
        # app.exec() より前の失敗（ウィンドウ構築中の例外など）も同じ経路で知らせる。
        # exec 中のスロット例外は Qt が握って外に出ないので、対象は起動時だけ。
        # sys.exit(app.exec()) の SystemExit は Exception ではないので通り抜ける。
        crashlog.report_startup_failure(_e)
        sys.exit(1)
