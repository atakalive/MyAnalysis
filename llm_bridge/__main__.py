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

    sub.add_parser("active", help="Print active tab name and currently open dataset")
    sub.add_parser("list-analyses", help="List analyses/ subdirs")
    p_lds = sub.add_parser("list-datasets", help="List registered dataset names")
    p_lds.add_argument("--json", action="store_true", dest="json_out", default=False,
                        help="Output as JSON with format and path info")

    p_reg = sub.add_parser("register-dataset", help="Register a dataset in config.py")
    p_reg.add_argument("name", help="Dataset name (identifier format)")
    p_reg.add_argument("path", help="Absolute path to dataset directory")
    p_reg.add_argument("--host", default=None, help="Hostname (default: current host)")
    p_reg.add_argument(
        "--with-analysis", nargs="?", const="", default=None,
        metavar="ANALYSIS_NAME",
        help="Also create analysis scaffold (default name = dataset name)",
    )
    p_reg.add_argument(
        "--format", default="csv_per_subdir",
        choices=("csv_per_subdir", "custom"),
        help="Dataset format (default: csv_per_subdir)",
    )
    p_reg.add_argument(
        "--no-open", action="store_true", default=False,
        help="Don't auto-open the dataset in a running GUI (skip the 10s wait)",
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
        if not args.json_out:
            for name in sorted(DATASETS):
                print(name)
            return 0
        from dataset_config import load_config
        host = socket.gethostname().upper()
        result = []
        for name in sorted(DATASETS):
            entry = {"name": name}
            per_host = DATASETS[name]
            entry["path"] = per_host.get(host)
            try:
                entry["format"] = load_config(name).get("format", "csv_per_subdir")
            except Exception as e:
                entry["format"] = None
                print(f"warning: could not read format for {name!r}: {e}", file=sys.stderr)
            result.append(entry)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "register-dataset":
        from config import register_dataset

        try:
            result = register_dataset(name=args.name, path=args.path, host=args.host)
        except (ValueError, SyntaxError, TypeError) as e:
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

        # register_dataset() は "file write only, no in-memory mutation" なので、
        # set_format → ensure_config → get_dataset_dir が新規データセットを
        # in-memory DATASETS で参照できるよう、先に reload する。
        from config import reload_datasets
        reload_datasets()

        if is_current_host and Path(args.path).exists():
            try:
                from dataset_config import set_format
                set_format(args.name, args.format)
            except Exception as e:
                print(f"warning: could not set format in myanalysis.toml: {e}", file=sys.stderr)

        if args.with_analysis is not None:
            from newanalysis.__main__ import create_analysis

            analysis_name = args.with_analysis or args.name
            try:
                create_analysis(analysis_name, dataset=args.name, fmt=args.format)
                print(f"created analyses/{analysis_name}/")
            except (ValueError, FileExistsError) as e:
                print(f"error creating analysis: {e}", file=sys.stderr)

        verb = "registered" if result["created"] else "updated"
        print(
            f"{verb} dataset {result['name']!r} for host "
            f"{result['host']!r}: {result['path']}"
        )

        # Auto-open in a running GUI (best-effort).
        # Path(args.path).exists() は判定に使わない — Windows/WSL 混在環境では
        # WSL 上で Windows パスが常に False になり、サイレントスキップになるため。
        # パスの実在確認は GUI 側の open_dataset 内で行われる（dataset_dir.is_dir()）。
        if not getattr(args, "no_open", False) and is_current_host:
            try:
                cmd_id = commands.submit("window", None, "open-dataset", {"name": args.name})
                result = commands.wait_for(cmd_id, timeout=10.0)
                if result is None:
                    print(
                        "GUI からの応答なし（GUI 未起動、または復元処理中の可能性）。\n"
                        "GUI 側で python -m llm_bridge window open-dataset "
                        f"name={args.name} --wait で開けます。",
                        file=sys.stderr,
                    )
                elif result.get("status") == "ok":
                    r = result.get("result", "")
                    if isinstance(r, str) and r.startswith("error:"):
                        print(f"registered but could not open: {r}", file=sys.stderr)
                    elif isinstance(r, str) and r.startswith("no-session:"):
                        print(f"opened (no saved session)")
                    elif isinstance(r, str) and r.startswith("restored:"):
                        n = r.split(":", 1)[1]
                        print(f"opened (restored {n} tab(s))")
                    else:
                        print(f"opened: {r}")
                else:
                    # status != "ok" (error, rejected, stale, malformed, etc.)
                    print(
                        f"registered but could not open dataset: {result}",
                        file=sys.stderr,
                    )
            except Exception as e:
                print(f"auto-open failed: {e}", file=sys.stderr)
        return 0

    if args.cmd == "list-commands":
        if args.name is None:
            print("window verbs (built-in by llm_bridge):")
            for v in ("add-tab", "close-tab", "open-dataset", "set-active-tab",
                      "show", "toggle-chat-float", "reload"):
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
