"""Headless PNG export driver.

Usage: python -m export <dataset> <analysis_name>

Writes figures to <work_dir>/analyses/<name>/batch/.
"""

import argparse
import sys

# Agg backend を analysis.py の matplotlib import より先に確定させる。
# core.figures は import 時に matplotlib.use("Agg") を実行する (core/figures.py:4-5)。
from core import figures  # noqa: F401 — side-effect import for Agg
import dataset_config

import importlib.util


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export analysis figures as headless PNG",
    )
    parser.add_argument("dataset", help="dataset name (config.DATASETS key)")
    parser.add_argument(
        "name", help="analysis directory name (e.g. example_analysis)"
    )
    args = parser.parse_args()
    dataset: str = args.dataset
    name: str = args.name

    try:
        analysis_file = dataset_config.analysis_file(dataset, name)
    except (ValueError, KeyError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
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

    if not hasattr(mod, "load"):
        print(f"error: {analysis_file} has no load()", file=sys.stderr)
        sys.exit(1)
    try:
        data = mod.load()
    except Exception as e:
        print(f"error: failed to load analysis data: {e}", file=sys.stderr)
        sys.exit(1)
    if data is None:
        print(f"error: {analysis_file} load() returned None", file=sys.stderr)
        sys.exit(1)
    figs: dict[str, object] = mod.build_export_figs(data)
    out = dataset_config.batch_dir(dataset, name)
    for filename, fig in figs.items():
        figures.save(fig, out / filename)
    print(f"saved {len(figs)} figure(s) to {out}")


if __name__ == "__main__":
    main()
