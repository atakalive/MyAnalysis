"""gui/math_engine.py — チャットの数式の TeX → SVG 変換エンジン（Issue #123）。

本物の QtWebEngine と導入済みの MathJax を使うテストは、pytest のプロセスに Chromium を入れないよう
``AA_ShareOpenGLContexts`` を設定した子プロセスで動かす。導入先は環境変数
``MYANALYSIS_MATHJAX_TEST_ROOT``（無ければ skip）。指定時の起動失敗はテスト失敗にする。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from test_mathmd import SAMPLE_MD

REPO = Path(__file__).resolve().parent.parent
REAL_FONTS = ("mathjax-dejavu", "mathjax-newcm")


def _child_env(**extra: str) -> dict[str, str]:
    """子プロセスの env。親の PYTHONPATH は消さず、その前にリポジトリを足す（依存を PYTHONPATH で
    読んでいる環境でも、子が依存を見失わない）。"""
    pp = os.environ.get("PYTHONPATH", "")
    return {**os.environ, **extra,
            "PYTHONPATH": os.pathsep.join(x for x in (str(REPO), pp) if x)}


# ---------------------------------------------------------------- is_allowed_url（導入先が無くても走る）

def test_is_allowed_url(tmp_path):
    from PySide6.QtCore import QUrl
    from gui.math_engine import is_allowed_url
    host = tmp_path / "assets" / "mathjax_host.html"
    host.parent.mkdir()
    host.write_text("x")
    root = tmp_path / "mj" / "4.1.3"
    (root / "input").mkdir(parents=True)

    def fu(p):
        return QUrl.fromLocalFile(str(p)).toString()

    assert is_allowed_url(fu(host) + "?root=file:///x&font=a", host=host, root=root)
    assert not is_allowed_url(fu(host.parent / "other.html"), host=host, root=root)
    assert is_allowed_url(fu(root / "tex-svg.js"), host=host, root=root)
    assert is_allowed_url(fu(root / "input" / "a.js"), host=host, root=root)
    assert not is_allowed_url(fu(root) + "/../../secret.js", host=host, root=root)
    assert not is_allowed_url(fu(tmp_path / "mj" / "4.1.3x" / "a.js"), host=host, root=root)
    for bad in ("https://cdn.jsdelivr.net/npm/mathjax@4/tex-svg.js",
                "data:text/javascript,alert(1)", "about:blank"):
        assert not is_allowed_url(bad, host=host, root=root)


def test_child_env_keeps_parent_pythonpath(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(["/deps/a", "/deps/b"]))
    env = _child_env(QT_QPA_PLATFORM="offscreen")
    assert env["PYTHONPATH"] == os.pathsep.join([str(REPO), "/deps/a", "/deps/b"])
    assert env["QT_QPA_PLATFORM"] == "offscreen"
    monkeypatch.delenv("PYTHONPATH")
    assert _child_env()["PYTHONPATH"] == str(REPO)


def test_svg_cache_counts_utf8_bytes(monkeypatch, qapp_fixture):
    from gui import math_engine
    from gui.math_engine import MathEngine
    eng = MathEngine()
    key = ("mathjax-dejavu", "\u03b1", False)
    eng._put(key, "svg", "\u3042" * 10)                 # 10 文字・30 バイト
    assert eng._cache_bytes == 30 + 2
    eng._put(key, "svg", "x")                           # 置き換えで前の分を引く
    assert eng._cache_bytes == 1 + 2
    monkeypatch.setattr(math_engine, "SVG_CACHE_BYTES", 100)
    eng._put(("mathjax-dejavu", "a", False), "svg", "\u3042" * 20)   # 20 文字・60 バイト
    eng._put(("mathjax-dejavu", "b", False), "svg", "\u3042" * 20)
    # 文字数なら 40+…で上限の内だが、バイト数（60+1 を 2 件）では超えるので古いものから捨てる
    assert ("mathjax-dejavu", "a", False) not in eng._cache
    assert ("mathjax-dejavu", "b", False) in eng._cache
    assert eng._cache_bytes == 61
    eng.stop()


# ---------------------------------------------------------------- 遅延 import

def test_webengine_not_imported_by_gui_modules():
    code = ("import os, sys; os.environ['QT_QPA_PLATFORM']='offscreen';"
            "import gui.chat, gui.math_settings_dialog, gui.math_engine, gui.math_render;"
            "print('PySide6.QtWebEngineCore' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True,
                         env=_child_env(), timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "False"


@pytest.mark.parametrize("share_contexts", [False, True])
def test_webengine_startup_attribute_in_child(tmp_path, share_contexts):
    code = textwrap.dedent(r'''
        import sys
        from pathlib import Path
        from PySide6.QtCore import QCoreApplication, Qt
        share_contexts = sys.argv[2] == "True"
        if share_contexts:
            QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
        from PySide6.QtWidgets import QApplication
        app = QApplication([])
        from common import mathjax_install
        from llm_bridge import paths as lb_paths
        root = Path(sys.argv[1])
        lb_paths.ui_prefs_path = lambda: root / "ui_prefs.json"
        mathjax_install.install_root = lambda: root / "mathjax"
        from gui import math_engine as me
        assert "PySide6.QtWebEngineCore" not in sys.modules
        if share_contexts:
            mod = me._load_webengine()
            assert mod is sys.modules["PySide6.QtWebEngineCore"]
        else:
            try:
                me._load_webengine()
            except me.RestartRequired:
                pass
            else:
                raise AssertionError("missing RestartRequired")

            def no_acquire(*args):
                raise AssertionError("must not acquire a use lock")
            mathjax_install.acquire_use = no_acquire
            for via in ("request", "probe"):
                eng = me.MathEngine()
                eng.prefs = me.MathPrefs(enabled=True)
                try:
                    if via == "probe":
                        assert eng.webengine_ok() is False
                        assert eng.unavailable_reason == "restart"
                    for font in ("mathjax-dejavu", "mathjax-newcm"):
                        eng.reset()
                        assert eng.unavailable_reason is None
                        eng.prefs = me.MathPrefs(enabled=False, font=font)
                        assert eng.request("disabled", False).error == "unavailable"
                        eng.prefs = me.MathPrefs(enabled=True, font=font)
                        for force in (False, True):
                            assert eng.request("x", False, force=force) == me.MathResult("error", error="unavailable")
                            assert eng.state == "unavailable"
                            assert eng.unavailable_reason == "restart"
                            assert not eng.running
                            assert eng._profile is None and eng._use is None
                            assert not eng.is_pending((font, "x", False))
                    assert not eng.webengine_ok()
                finally:
                    eng.stop()
            assert "PySide6.QtWebEngineCore" not in sys.modules
            assert not (root / "mathjax").exists()
        print("OK")
    ''')
    out = subprocess.run([sys.executable, "-c", code, str(tmp_path), str(share_contexts)],
                         cwd=REPO, capture_output=True, text=True,
                         env=_child_env(QT_QPA_PLATFORM="offscreen"), timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.strip().splitlines()[-1] == "OK"


def _enable_prefs(**kw):
    from llm_bridge.paths import update_ui_pref
    update_ui_pref("math_render", True)
    for k, v in kw.items():
        update_ui_pref(k, v)


@pytest.mark.parametrize("error_type", [ImportError, OSError])
def test_webengine_import_failure_is_unavailable(monkeypatch, qapp_fixture, error_type):
    from gui import math_engine
    from gui.math_engine import MathEngine, MathResult

    def boom():
        raise error_type("no QtWebEngineCore")
    monkeypatch.setattr(math_engine, "_load_webengine", boom)
    _enable_prefs()
    eng = MathEngine()
    assert eng.request("x", False) == MathResult("error", None, "unavailable")
    assert eng.state == "unavailable"
    assert eng.unavailable_reason is None
    assert eng.request("y", True) == MathResult("error", None, "unavailable")
    eng.stop()


def test_import_failure_shows_in_settings_dialog(monkeypatch, qapp_fixture):
    from common.i18n import tr
    from gui import math_engine
    from gui.math_settings_dialog import MathSettingsDialog

    def boom():
        raise ImportError("no QtWebEngineCore")
    monkeypatch.setattr(math_engine, "_load_webengine", boom)
    eng = math_engine.MathEngine()
    dlg = MathSettingsDialog(engine=eng)
    try:
        assert dlg.status_text() == tr("math.status.no_webengine")
    finally:
        dlg.deleteLater()
        eng.stop()


def test_restart_required_shows_in_settings_dialog(monkeypatch, qapp_fixture):
    from common.i18n import tr
    from gui import math_engine
    from gui.math_settings_dialog import MathSettingsDialog

    def boom():
        raise math_engine.RestartRequired("restart MyAnalysis")
    monkeypatch.setattr(math_engine, "_load_webengine", boom)
    eng = math_engine.MathEngine()
    dlg = MathSettingsDialog(engine=eng)
    try:
        assert eng.state == "unavailable"
        assert eng.unavailable_reason == "restart"
        assert dlg.status_text() == tr("math.status.restart_required")
    finally:
        dlg.deleteLater()
        eng.stop()


def test_unavailable_reason_cleared_before_state_notification(qapp_fixture):
    from gui.math_engine import MathEngine
    eng = MathEngine()
    seen = []
    eng.stateChanged.connect(lambda: seen.append((eng.state, eng.last_error, eng.unavailable_reason)))
    try:
        assert eng.unavailable_reason is None
        eng._fail("restart", state="unavailable", reason="restart")
        assert seen[-1] == ("unavailable", "restart", "restart")
        eng.reset()
        assert seen[-1] == ("idle", None, None)
        eng._fail("restart", state="unavailable", reason="restart")
        eng._fail("other failure")
        assert seen[-1] == ("failed", "other failure", None)
    finally:
        eng.stop()


@pytest.fixture
def qapp_fixture():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_unavailable_not_cached_and_too_long_cached(qapp_fixture):
    from gui.math_engine import MAX_TEX, MathEngine, MathResult
    eng = MathEngine()        # 既定は無効
    assert eng.request("x", False) == MathResult("error", None, "unavailable")
    assert not eng._cache
    _enable_prefs()
    eng.reload_prefs()
    long_tex = "x" * (MAX_TEX + 1)
    assert eng.request(long_tex, False) == MathResult("error", None, "too_long")
    assert (eng.prefs.font, long_tex, False) in eng._cache
    eng.stop()


# ---------------------------------------------------------------- 本物の QtWebEngine と MathJax（子プロセス）

_CHILD = textwrap.dedent(r'''
    import json, os, sys, time
    from pathlib import Path
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtCore import QCoreApplication, Qt, QEventLoop, QTimer, qVersion
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    from PySide6.QtWebEngineCore import qWebEngineChromiumVersion
    import shiboken6
    from common import mathjax_install
    from llm_bridge import paths as lb_paths
    root = Path(os.environ["MYANALYSIS_MATHJAX_TEST_ROOT"])
    mathjax_install.install_root = lambda: root
    prefs = Path(sys.argv[1])
    lb_paths.ui_prefs_path = lambda: prefs
    from gui import math_engine
    from common.mathmd import split_math
    spans = json.loads(sys.argv[2])
    out = {"fonts": {}, "versions": {"python": sys.version, "qt": qVersion(),
                                    "chromium": qWebEngineChromiumVersion()}}

    def wait(cond, secs):
        t = time.monotonic()
        while time.monotonic() - t < secs:
            if cond():
                return True
            loop = QEventLoop(); QTimer.singleShot(20, loop.quit); loop.exec()
        return cond()

    eng = math_engine.MathEngine()
    for font in ("mathjax-dejavu", "mathjax-newcm"):
        prefs.write_text(json.dumps({"math_render": True, "math_font": font}))
        eng.reload_prefs()
        items = spans + [["\\undefinedcommand x", False]]
        res = {}
        def done():
            for tex, disp in items:
                r = eng.request(tex, disp)
                if r.status == "pending":
                    return False
                res[tex + "|" + str(disp)] = [r.status, r.svg, r.error]
            return True
        ok = wait(done, 60)
        out["fonts"][font] = {"ok": ok, "results": res, "state": eng.state, "error": eng.last_error}
        if eng.state in ("failed", "unavailable"):
            break
    if eng.state == "ready":
        prefs.write_text(json.dumps({"math_render": False, "math_font": font}))
        eng.reload_prefs()
        stopped = not eng.running and eng._profile is None and eng._use is None and not eng._cache
        prefs.write_text(json.dumps({"math_render": True, "math_font": font}))
        eng.reload_prefs()
        out["disable_enable"] = {"stopped": stopped, "ok": wait(done, 60),
                                 "state": eng.state, "reason": eng.unavailable_reason}
    # stop の順序
    order = []
    page, profile = eng._page, eng._profile
    if page is not None:
        page.destroyed.connect(lambda *_: order.append("page"))
        profile.destroyed.connect(lambda *_: order.append("profile"))
    eng.stop()
    eng.stop()
    out["order"] = order
    out["valid_after"] = [shiboken6.isValid(page) if page is not None else None,
                          shiboken6.isValid(profile) if profile is not None else None]
    out["rejected"] = eng.rejected
    out["console_errors"] = eng.console_errors
    out["inuse_left"] = sorted(p.name for p in (root / "inuse").glob("*.lock"))
    print("RESULT " + json.dumps(out))
''')


@pytest.fixture(scope="module")
def real_run(tmp_path_factory):
    root = os.environ.get("MYANALYSIS_MATHJAX_TEST_ROOT")
    if not root:
        pytest.skip("MYANALYSIS_MATHJAX_TEST_ROOT is not set")
    from common.mathmd import split_math
    spans = [[s.tex, s.display] for s in split_math(SAMPLE_MD)[1]]
    prefs = tmp_path_factory.mktemp("prefs") / "ui_prefs.json"
    p = subprocess.run([sys.executable, "-c", _CHILD, str(prefs), json.dumps(spans)],
                       cwd=REPO, capture_output=True, text=True, timeout=300,
                       env=_child_env(QT_QPA_PLATFORM="offscreen"))
    assert p.returncode == 0, (
        f"QtWebEngine child exited with {p.returncode}\n"
        f"stdout: {p.stdout[-2000:]}\nstderr: {p.stderr[-4000:]}"
    )
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")]
    assert lines, f"QtWebEngine returned no RESULT\nstdout: {p.stdout}\nstderr: {p.stderr}"
    data = json.loads(lines[-1][len("RESULT "):])
    first = data["fonts"].get("mathjax-dejavu", {})
    assert first.get("state") == "ready", (first, data.get("versions"), p.stderr)
    data["stderr"] = p.stderr
    data["spans"] = spans
    return data


@pytest.mark.parametrize("returncode,stdout,expected", [
    (1, 'RESULT {"fonts": {}}', "exited with 1"),
    (0, "", "no RESULT"),
    (0, 'RESULT {"fonts": {"mathjax-dejavu": {"state": "unavailable"}}}', "unavailable"),
])
def test_real_run_reports_startup_failures(monkeypatch, tmp_path_factory, returncode, stdout, expected):
    monkeypatch.setenv("MYANALYSIS_MATHJAX_TEST_ROOT", str(tmp_path_factory.getbasetemp() / "mathjax"))
    monkeypatch.setattr(
        subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], returncode, stdout, "startup detail"),
    )
    with pytest.raises(AssertionError, match=expected):
        real_run.__wrapped__(tmp_path_factory)


def _svgs(data, font):
    f = data["fonts"][font]
    assert f["ok"], f
    return {k: v for k, v in f["results"].items()}


def test_real_sample_converts_and_errors(real_run):
    res = _svgs(real_run, "mathjax-dejavu")
    for tex, disp in real_run["spans"]:
        status, svg, err = res[f"{tex}|{disp}"]
        assert status == "ready", (tex, err)
        assert svg.startswith("<svg") and 'stroke-width="0"' in svg
    status, svg, err = res["\\undefinedcommand x|False"]
    assert status == "error" and svg is None and err


def test_real_font_choice_is_effective(real_run):
    a = _svgs(real_run, "mathjax-dejavu")
    b = _svgs(real_run, "mathjax-newcm")
    tex, disp = real_run["spans"][2]
    key = f"{tex}|{disp}"
    assert a[key][1] != b[key][1]


def test_real_disable_enable_restarts_rendering(real_run):
    assert real_run["disable_enable"] == {"stopped": True, "ok": True, "state": "ready", "reason": None}


def test_real_no_rejected_urls_or_console_errors(real_run):
    assert real_run["rejected"] == []
    assert real_run["console_errors"] == []


def test_real_stop_order_and_cleanup(real_run):
    assert real_run["order"] == ["page", "profile"]
    assert real_run["valid_after"] == [False, False]
    assert real_run["inuse_left"] == []
    assert "Release of profile requested but WebEnginePage still not deleted" not in real_run["stderr"]
    assert "Traceback" not in real_run["stderr"]


def _effective(elem_chain, attr):
    for e in reversed(elem_chain):
        if attr in e.attrib:
            return e.attrib[attr]
    return None


def _walk(e, chain):
    chain = chain + [e]
    yield e, chain
    for c in e:
        yield from _walk(c, chain)


def _svg_xml(src, context):
    try:
        return ET.fromstring(src)
    except ET.ParseError as e:
        pytest.fail(f"invalid SVG XML {context!r}: {e}")


@pytest.mark.parametrize("font", REAL_FONTS)
def test_real_svg_xml_preserves_tex(real_run, font):
    res = _svgs(real_run, font)
    for tex, disp in real_run["spans"]:
        context = (font, tex, disp)
        root = _svg_xml(res[f"{tex}|{disp}"][1], context)
        math = root.find(".//*[@data-mml-node='math']")
        assert math is not None, context
        assert math.get("data-latex") == tex, context


@pytest.mark.parametrize("font", REAL_FONTS)
def test_real_bold_assumption_holds(real_run, font):
    from gui.math_render import prepare_svg
    res = _svgs(real_run, font)
    for tex, disp in real_run["spans"]:
        svg = res[f"{tex}|{disp}"][1]
        for src, want in ((prepare_svg(svg, "#ffffff", False), "0"), (prepare_svg(svg, "#ffffff", True), "15")):
            context = (font, tex, disp, want)
            root = _svg_xml(src, context)
            n = 0
            for e, chain in _walk(root, []):
                tag = e.tag.rsplit("}", 1)[-1]
                if tag not in ("path", "rect") or any(c.tag.endswith("defs") for c in chain):
                    continue
                n += 1
                assert _effective(chain, "stroke-width") == want, context
                assert _effective(chain, "stroke") not in (None, "none"), context
                if want == "15":
                    assert _effective(chain, "stroke-linejoin") == "round", context
                    assert _effective(chain, "stroke-linecap") == "round", context
            # path は defs の中で use から参照されるので、defs の中も確かめる
            for e, chain in _walk(root, []):
                if e.tag.rsplit("}", 1)[-1] == "use":
                    assert _effective(chain, "stroke-width") == want, context


@pytest.mark.parametrize("font", REAL_FONTS)
@pytest.mark.parametrize("bold", [False, True])
def test_real_svg_renders_to_pixels(real_run, qapp_fixture, font, bold):
    from PySide6.QtCore import QByteArray, QRectF, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    from gui.math_render import prepare_svg, svg_viewbox
    res = _svgs(real_run, font)
    for tex, disp in real_run["spans"]:
        context = (font, tex, disp, bold)
        svg = prepare_svg(res[f"{tex}|{disp}"][1], "#ffffff", bold)
        r = QSvgRenderer(QByteArray(svg.encode()))
        assert r.isValid(), context
        vb = svg_viewbox(svg)
        assert vb is not None, context
        _, _, w, h = vb
        img = QImage(max(1, round(w / 50)), max(1, round(h / 50)), QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        r.render(p, QRectF(0, 0, img.width(), img.height()))
        p.end()
        assert any(img.pixelColor(x, y).alpha() > 0
                   for x in range(img.width()) for y in range(img.height())), context
