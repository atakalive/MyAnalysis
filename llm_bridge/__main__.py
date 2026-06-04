"""CLI for LLM operation: python -m llm_bridge <subcommand> ..."""
import argparse
import json
import socket
import sys
from pathlib import Path

from common.paths import analyses_root
from llm_bridge import state, annotations, commands
from llm_bridge.paths import active_state_path


def _parse_kvs(kvs: list[str]) -> dict:
    """Parse [k=v, ...] into dict, with int → float → str type coercion."""
    out: dict = {}
    for kv in kvs:
        if "=" not in kv:
            raise SystemExit(f"error: expected key=value, got: {kv!r}")
        k, v = kv.split("=", 1)
        try:
            out[k] = int(v)
        except ValueError:
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
    return out


def _check_tab_name(name: str) -> None:
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise SystemExit(f"error: invalid tab name: {name!r}")


def _check_analysis_exists(name: str) -> None:
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise SystemExit(f"error: invalid analysis name: {name!r}")
    root = analyses_root().resolve()
    analysis_dir = (analyses_root() / name).resolve()
    if not analysis_dir.is_relative_to(root):
        raise SystemExit(f"error: analysis directory escapes analyses/: {name!r}")
    if not analysis_dir.is_dir():
        raise SystemExit(f"error: no analysis named {name!r} under analyses/")
    analysis_file = (analysis_dir / "analysis.py").resolve()
    if not analysis_file.is_relative_to(root) or not analysis_file.is_file():
        raise SystemExit(f"error: no valid analysis.py for {name!r} under analyses/")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m llm_bridge")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_state = sub.add_parser("state", help="Print state.json for an analysis")
    p_state.add_argument("name", nargs="?")

    sub.add_parser("active", help="Print currently active tab name")
    sub.add_parser("list-analyses", help="List analyses/ subdirs")
    sub.add_parser("list-datasets", help="List registered dataset names")

    p_reg = sub.add_parser("register-dataset", help="Register a dataset in config.py")
    p_reg.add_argument("name", help="Dataset name (identifier format)")
    p_reg.add_argument("path", help="Absolute path to dataset directory")
    p_reg.add_argument("--host", default=None, help="Hostname (default: current host)")
    p_reg.add_argument(
        "--with-analysis", nargs="?", const="", default=None,
        metavar="ANALYSIS_NAME",
        help="Also create analysis scaffold (default name = dataset name)",
    )

    p_lc = sub.add_parser("list-commands", help="List registered verbs (informational)")
    p_lc.add_argument("name", nargs="?")

    p_win = sub.add_parser("window", help="Window-tier command")
    p_win.add_argument("verb")
    p_win.add_argument("kvs", nargs="*")
    p_win.add_argument("--wait", nargs="?", type=float, const=30.0, default=None)

    p_tab = sub.add_parser("tab", help="Tab-tier command")
    p_tab.add_argument("target")
    p_tab.add_argument("verb")
    p_tab.add_argument("kvs", nargs="*")
    p_tab.add_argument("--wait", nargs="?", type=float, const=30.0, default=None)

    p_ann = sub.add_parser("annotate", help="Add an annotation")
    p_ann.add_argument("name")
    p_ann.add_argument("kind", choices=["marker", "note"])
    p_ann.add_argument("kvs", nargs="*")

    p_clr = sub.add_parser("clear-annotations", help="Clear annotations")
    p_clr.add_argument("name")
    p_clr.add_argument("kind", nargs="?", choices=["marker", "note"], default=None)

    args = parser.parse_args(argv)

    if args.cmd == "state":
        if args.name is None:
            active = json.loads(active_state_path().read_text(encoding="utf-8")) \
                if active_state_path().exists() else {}
            if not active.get("active_tab"):
                print(json.dumps({}))
                return 0
            print(json.dumps(state.read(active["active_tab"]),
                             ensure_ascii=False, indent=2))
        else:
            _check_analysis_exists(args.name)
            print(json.dumps(state.read(args.name), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "active":
        p = active_state_path()
        if p.exists():
            print(p.read_text(encoding="utf-8"))
        else:
            print(json.dumps({"active_tab": None}))
        return 0

    if args.cmd == "list-analyses":
        root = analyses_root().resolve()
        for d in sorted(analyses_root().glob("*")):
            if d.name.startswith("_"):
                continue
            resolved = d.resolve()
            if not resolved.is_relative_to(root):
                continue
            af = (resolved / "analysis.py").resolve()
            if resolved.is_dir() and af.is_relative_to(root) and af.is_file():
                print(d.name)
        return 0

    if args.cmd == "list-datasets":
        from config import DATASETS
        for name in sorted(DATASETS):
            print(name)
        return 0

    if args.cmd == "register-dataset":
        from config import register_dataset

        try:
            result = register_dataset(name=args.name, path=args.path, host=args.host)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

        is_current_host = (
            args.host is None
            or args.host.upper() == socket.gethostname().upper()
        )
        if is_current_host and not Path(args.path).exists():
            print(
                f"warning: path does not exist on this host: {args.path}",
                file=sys.stderr,
            )

        if args.with_analysis is not None:
            from newanalysis.__main__ import create_analysis

            analysis_name = args.with_analysis or args.name
            try:
                create_analysis(analysis_name, dataset=args.name)
                print(f"created analyses/{analysis_name}/")
            except (ValueError, FileExistsError) as e:
                print(f"error creating analysis: {e}", file=sys.stderr)

        verb = "registered" if result["created"] else "updated"
        print(
            f"{verb} dataset {result['name']!r} for host "
            f"{result['host']!r}: {result['path']}"
        )
        return 0

    if args.cmd == "list-commands":
        if args.name is None:
            print("window verbs (built-in by llm_bridge):")
            for v in ("add-tab", "close-tab", "set-active-tab", "show",
                      "toggle-chat-float"):
                print(f"  {v}")
            return 0
        _check_tab_name(args.name)
        print("tab verbs (built-in by llm_bridge):")
        for v in ("set-split", "snapshot", "refresh-state"):
            print(f"  {v}")
        print("(analysis-specific verbs also registered at runtime — "
              "see analysis source)")
        print("Viewer tabs: use `show slot=left|right|top|bottom` for "
              "side-by-side layout.")
        return 0

    if args.cmd == "window":
        kwargs = _parse_kvs(args.kvs)
        cmd_id = commands.submit("window", None, args.verb, kwargs)
        if args.wait is not None:
            result = commands.wait_for(cmd_id, timeout=args.wait)
            if result is None:
                print(f"timeout waiting for {cmd_id}", file=sys.stderr)
                return 1
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(cmd_id)
        return 0

    if args.cmd == "tab":
        _check_tab_name(args.target)
        kwargs = _parse_kvs(args.kvs)
        cmd_id = commands.submit("tab", args.target, args.verb, kwargs)
        if args.wait is not None:
            result = commands.wait_for(cmd_id, timeout=args.wait)
            if result is None:
                print(f"timeout waiting for {cmd_id}", file=sys.stderr)
                return 1
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(cmd_id)
        return 0

    if args.cmd == "annotate":
        _check_analysis_exists(args.name)
        fields = _parse_kvs(args.kvs)
        annotations.submit(args.name, args.kind, **fields)
        return 0

    if args.cmd == "clear-annotations":
        _check_analysis_exists(args.name)
        annotations.clear(args.name, args.kind)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
