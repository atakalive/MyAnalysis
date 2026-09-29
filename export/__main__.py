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
from common.analysis_module import analysis_module, analysis_module_name
from common.paths import pycache_prefix


def main() -> None:
    # 解析が同期マウント上の別の .py を import したときの .pyc もローカルへ逃がす（D-8）
    sys.pycache_prefix = sys.pycache_prefix or str(pycache_prefix())
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

    # export だけ utf-8-sig で読む: 旧実装はローダーがバイト列から読むので BOM 付きも
    # 通っていた。str にしてから compile すると U+FEFF で SyntaxError になるため剥がす。
    try:
        source = analysis_file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        print(f"error: could not read {analysis_file}: {e}", file=sys.stderr)
        sys.exit(1)

    with analysis_module(analysis_module_name(dataset, name), analysis_file, source) as mod:
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
