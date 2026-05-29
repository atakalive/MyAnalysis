"""Headless PNG export driver.

Usage: python -m export <analysis_name>

Writes figures to data/analyses/<name>/batch/.
"""
import argparse
import sys

# Agg backend を analysis.py の matplotlib import より先に確定させる。
# core.figures は import 時に matplotlib.use("Agg") を実行する (core/figures.py:4-5)。
from core import figures  # noqa: F401 — side-effect import for Agg
from common.paths import analyses_root, batch_dir

import importlib.util


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export analysis figures as headless PNG",
    )
    parser.add_argument("name", help="analysis directory name (e.g. example_analysis)")
    args = parser.parse_args()
    name: str = args.name

    # simple-name 制約: batch_dir(name) は common.paths._validate_name と同じ制約
    # (空文字・/・\・.・.. を拒否) を持つ。resolve+is_relative_to だけでは
    # "foo/../bar" のような / 含みの名前が root 内に解決されて通過し、
    # batch_dir 側の ValueError で unhandled traceback になる。先にチェックする。
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        print(f"error: invalid analysis name: {name!r}", file=sys.stderr)
        sys.exit(1)

    # パス安全性: analyses_root() は repo_root 基準の絶対パス (common/paths.py:27)。
    # resolve + is_relative_to で traversal を防止。
    # llm_bridge.__init__._make_add_tab_handler (60-63行目) と同等の 2 段階チェック。
    root = analyses_root().resolve()
    analysis_dir = (analyses_root() / name).resolve()
    analysis_file = (analysis_dir / "analysis.py").resolve()
    if not analysis_dir.is_relative_to(root):
        print(f"error: name escapes analyses/: {name!r}", file=sys.stderr)
        sys.exit(1)
    if not analysis_file.is_relative_to(root):
        print(f"error: analysis.py escapes analyses/: {name!r}", file=sys.stderr)
        sys.exit(1)
    if not analysis_file.is_file():
        print(f"error: {analysis_file} not found", file=sys.stderr)
        sys.exit(1)

    spec = importlib.util.spec_from_file_location(f"_export_{name}", str(analysis_file))
    if spec is None or spec.loader is None:
        print(f"error: could not load {analysis_file}", file=sys.stderr)
        sys.exit(1)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    if not hasattr(mod, "build_export_figs"):
        print(f"error: {analysis_file} has no build_export_figs(data)", file=sys.stderr)
        sys.exit(1)

    data = mod.load() if hasattr(mod, "load") else None
    if data is None:
        print(f"error: {analysis_file} has no load() or load() returned None", file=sys.stderr)
        sys.exit(1)
    figs: dict[str, object] = mod.build_export_figs(data)
    out = batch_dir(name)
    for filename, fig in figs.items():
        figures.save(fig, out / filename)
    print(f"saved {len(figs)} figure(s) to {out}")


if __name__ == "__main__":
    main()
