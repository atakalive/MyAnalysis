"""CLI for LLM operation: python -m llm_bridge <subcommand> ..."""
import argparse
import json
import math
import os
import re
import socket
import sys
from pathlib import Path

import dataset_config
from common.chat_dataset import CHAT_DATASET_ENV, chat_dataset_value
from common.paths import safe_resolve
from llm_bridge import state, annotations, commands
from llm_bridge.paths import active_state_path


# 名前・パス・識別子・自由文のキー。数値に見えても変換しない（Issue #100 D-6）。
_STRING_KEYS: frozenset[str] = frozenset({
    "name", "dataset", "target", "path", "scope", "slot", "panel", "mode", "lut",
    "text", "label", "color", "sender", "session", "query", "sid", "search_tab",
})
_INT_RE = re.compile(r"[+-]?[0-9]+")
_FLOAT_RE = re.compile(r"[+-]?(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)(?:[eE][+-]?[0-9]+)?")


def _parse_kvs(kvs: list[str]) -> dict:
    """Parse [k=v, ...] into dict. Values of `_STRING_KEYS` stay str; others
    become int / float only when they fully match a plain ASCII decimal literal
    (finite); everything else stays str."""
    out: dict = {}
    for kv in kvs:
        if "=" not in kv:
            raise SystemExit(f"error: expected key=value, got: {kv!r}")
        k, v = kv.split("=", 1)
        out[k] = v
        if k in _STRING_KEYS:
            continue
        if _INT_RE.fullmatch(v):
            try:
                out[k] = int(v)
            except ValueError:  # 桁数上限（4300 桁）超え
                pass
        elif _FLOAT_RE.fullmatch(v):
            f = float(v)
            if math.isfinite(f):
                out[k] = f
    return out


def _check_tab_name(name: str) -> None:
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise SystemExit(f"error: invalid tab name: {name!r}")


def _check_analysis_exists(dataset: str, name: str) -> None:
    try:
        analysis_file = dataset_config.analysis_file(dataset, name)
    except (ValueError, KeyError, RuntimeError) as e:
        raise SystemExit(f"error: {e}")
    if not analysis_file.is_file():
        raise SystemExit(f"error: no analysis named {name!r} under {dataset!r}")


def _list_analysis_names(dataset: str) -> list[str]:
    """Containment-checked analysis names under one dataset (sorted, _ prefixed skipped)."""
    try:
        root = dataset_config.analyses_root(dataset)
    except (KeyError, RuntimeError):
        return []
    if not root.is_dir():
        return []
    root_resolved = safe_resolve(root)
    names = []
    for d in sorted(root.glob("*")):
        if d.name.startswith("_"):
            continue
        resolved = safe_resolve(d)
        if not resolved.is_relative_to(root_resolved):
            continue
        af = safe_resolve(resolved / "analysis.py")
        if resolved.is_dir() and af.is_relative_to(root_resolved) and af.is_file():
            names.append(d.name)
    return names


def _chat_dataset_env() -> str | None:
    """チャットのエージェントから実行されたときのチャットの DS（Issue #111）。無ければ None。"""
    return chat_dataset_value(os.environ.get(CHAT_DATASET_ENV))


def _submit(tier: str, target: str | None, verb: str, kwargs: dict) -> str:
    """チャットの DS があるときだけ caller_dataset を付けて submit する。"""
    chat_ds = _chat_dataset_env()
    if chat_ds is None:
        return commands.submit(tier, target, verb, kwargs)
    return commands.submit(tier, target, verb, kwargs, caller_dataset=chat_ds)


def _resolve_dataset(args, active: dict | None = None, *,
                     use_active_analysis: bool = False) -> str | None:
    """優先順: --dataset 明示 → チャットの DS（環境変数。Issue #111）→ active.json の
    該当フィールド。解決不能なら None。

    明示した --dataset が有効な DS 名でなければ（空文字など）SystemExit。
    use_active_analysis=True（`state` で name 省略）のときは環境変数を見ない（前面のまま）。
    GUI で DS が開いているかは見ない（ファイルを直接読み書きする verb 用）。"""
    ds = getattr(args, "dataset", None)
    if ds is not None:
        if chat_dataset_value(ds) is None:
            raise SystemExit(
                f"error: --dataset must be a non-empty dataset name, got {ds!r}")
        return ds
    if not use_active_analysis:
        chat_ds = _chat_dataset_env()
        if chat_ds is not None:
            return chat_ds
    if active is None:
        active = json.loads(active_state_path().read_text(encoding="utf-8")) \
            if active_state_path().exists() else {}
    return active.get("active_analysis_dataset") if use_active_analysis \
        else active.get("dataset")


def _harden_stdio() -> None:
    """パイプ・リダイレクト先の文字コード（日本語版 Windows では cp932）で書けない文字を
    ``\\uXXXX`` に置き換えて出す。全 verb の print と stderr が UnicodeEncodeError で落ちないようにする。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)   # None（pythonw）・StringIO には無い
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="backslashreplace")
        except (ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _harden_stdio()
    parser = argparse.ArgumentParser(prog="python -m llm_bridge")
    # metavar は choices の自動列挙（{state,active,…}）を抑える。help= を渡さない
    # サブコマンドを --help から完全に隠すために必要（metavar が無いと usage 行に
    # 名前が残る)。argparse は 'help' in kwargs のときだけ choices 行を登録する。
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="<subcommand>")

    p_state = sub.add_parser("state", help="Print state.json for an analysis")
    p_state.add_argument("name", nargs="?")
    p_state.add_argument("--dataset", default=None)

    sub.add_parser("active", help="Print active tab name and currently open dataset")
    p_la = sub.add_parser(
        "list-analyses",
        help="List analyses across all open datasets (or one via --dataset)")
    p_la.add_argument("--dataset", default=None)
    p_la.add_argument("--json", action="store_true", dest="json_out", default=False,
                      help="Output as JSON map {dataset: [names]}")
    sub.add_parser(
        "list-open-datasets",
        help="List datasets open in the running GUI (reads active.json)")
    p_lds = sub.add_parser("list-datasets", help="List registered dataset names")
    p_lds.add_argument("--json", action="store_true", dest="json_out", default=False,
                        help="Output as JSON with format and path info")

    p_reg = sub.add_parser("register-dataset", help="Register a dataset in datasets.local.json")
    p_reg.add_argument("name", help="Dataset name (identifier format)")
    p_reg.add_argument("path", help="Absolute path to dataset directory")
    p_reg.add_argument("--host", default=None, help="Hostname (default: current host)")
    p_reg.add_argument(
        "--format", default=None,
        choices=("csv_per_subdir", "custom"),
        help="Dataset format written to myanalysis.toml. Omit to keep the existing "
             "value (a new file gets csv_per_subdir)",
    )
    p_reg.add_argument(
        "--no-open", action="store_true", default=False,
        help="Don't auto-open the dataset in a running GUI (skip the 10s wait)",
    )

    # description / completed はユーザーが決める値。help= を省いて --help から隠し、
    # エージェントが verb を発見して勝手に meta.json を書きに行くのを防ぐ（verb 自体は
    # 人手/スクリプト用に残す。GUI の「概要を編集」は patch_description を直接呼ぶ）。
    p_sd = sub.add_parser("set-description")
    p_sd.add_argument("dataset")
    p_sd.add_argument("text")

    p_sc = sub.add_parser("set-completed")
    p_sc.add_argument("dataset")
    p_sc.add_argument("--off", action="store_true", default=False,
                      help="Unmark (set completed=False)")

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
    p_ann.add_argument("--dataset", default=None)

    p_clr = sub.add_parser("clear-annotations", help="Clear annotations")
    p_clr.add_argument("name")
    p_clr.add_argument("kind", nargs="?", choices=["marker", "note"], default=None)
    p_clr.add_argument("--dataset", default=None)

    for _verb, _help in (
        ("draft-analysis", "Copy analysis.py to an editable draft (mount-safe)"),
        ("apply-analysis", "Validate the draft and promote it to analysis.py"),
        ("recover-analysis", "Restore a 0-byte/absent analysis.py from its .bak"),
    ):
        _p = sub.add_parser(_verb, help=_help)
        _p.add_argument("name")
        _p.add_argument("--dataset", default=None)

    # PreToolUse hook から呼ばれる内部 verb（人が直接使うものではないので help を出さない）
    sub.add_parser("guard-write")

    sub.add_parser(
        "engines", help="対応 LLM ドライバの導入状況を一覧（GUI の「バックエンドの状況」と同じ）")
    p_doc = sub.add_parser(
        "doctor", help="同期マウント上のデータ健全性をチェック（Issue #96）")
    p_doc.add_argument("--dataset", default=None)
    p_doc.add_argument("--repair", action="store_true", default=False,
                       help="primary/.bak の乖離を newest-wins で収束させる")
    p_doc.add_argument("--rescue", action="store_true", default=False,
                       help="0 バイトファイルを rclone キャッシュの孤児 tmp から復元する")
    p_doc.add_argument("--cache", default=None, help="rclone の VFS キャッシュディレクトリ（省略時は環境変数 MYANALYSIS_RCLONE_CACHE）")
    p_doc.add_argument("--log", default=None, help="rclone のログファイル（省略時は環境変数 MYANALYSIS_RCLONE_LOG）")

    p_csync = sub.add_parser("config-sync", help="R2 と設定を双方向同期（収束）")
    p_csync.add_argument("--dry-run", action="store_true", default=False)
    p_csync.add_argument("--include-env", action="store_true", default=False)
    p_csync.add_argument("--key", default=None,
                         help="R2 オブジェクトキー上書き（既定: <R2_PREFIX>bundle.json）")

    p_cpush = sub.add_parser("config-push", help="マージ後 R2 のみへ書く")
    p_cpush.add_argument("--dry-run", action="store_true", default=False)
    p_cpush.add_argument("--include-env", action="store_true", default=False)
    p_cpush.add_argument("--key", default=None)

    p_cpull = sub.add_parser("config-pull", help="R2->ローカルのみ反映（remote は書かない）")
    p_cpull.add_argument("--dry-run", action="store_true", default=False)
    p_cpull.add_argument("--key", default=None)

    args = parser.parse_args(argv)

    if args.cmd == "state":
        if args.name is None:
            active = json.loads(active_state_path().read_text(encoding="utf-8")) \
                if active_state_path().exists() else {}
            if not active.get("active_tab"):
                print(json.dumps({}))
                return 0
            ds = _resolve_dataset(args, active, use_active_analysis=True)
            if ds is None:
                print(json.dumps({}))
                return 0
            print(json.dumps(state.read(ds, active["active_tab"]),
                             ensure_ascii=False, indent=2))
        else:
            ds = _resolve_dataset(args)
            if ds is None:
                raise SystemExit("error: no dataset open; pass --dataset")
            _check_analysis_exists(ds, args.name)
            print(json.dumps(state.read(ds, args.name), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "active":
        p = active_state_path()
        chat_ds = _chat_dataset_env()
        if not p.exists():
            print(json.dumps({"active_tab": None, "chat_dataset": chat_ds},
                             ensure_ascii=False))
            return 0
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict):
            # 読めない・壊れている・dict でないときも chat_dataset は必ず出す（規則文の
            # 「active は常に chat_dataset を返す」を守る）。
            data = {"active_tab": None, "error": "active.json is unreadable"}
        data["chat_dataset"] = chat_ds
        print(json.dumps(data, ensure_ascii=False))
        return 0

    if args.cmd == "list-analyses":
        # Targets: --dataset overrides; else all datasets open in the GUI
        # (active.json's open_datasets), falling back to the single active one.
        if args.dataset:
            targets = [args.dataset]
        else:
            active = json.loads(active_state_path().read_text(encoding="utf-8")) \
                if active_state_path().exists() else {}
            targets = active.get("open_datasets") or []
            if not targets and active.get("dataset"):
                targets = [active["dataset"]]
        if not targets:
            print("no dataset open; pass --dataset", file=sys.stderr)
            return 0
        result = {ds: _list_analysis_names(ds) for ds in targets}
        if args.json_out:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            for ds in targets:
                for n in result[ds]:
                    print(n)
        return 0

    if args.cmd == "list-open-datasets":
        active = json.loads(active_state_path().read_text(encoding="utf-8")) \
            if active_state_path().exists() else {}
        print(json.dumps(
            {"open": active.get("open_datasets") or [],
             "active": active.get("active_dataset") or active.get("dataset")},
            ensure_ascii=False,
        ))
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
            from llm_bridge import dataset_meta
            meta = dataset_meta.read_meta(name) or {}
            entry["description"] = meta.get("description", "")
            entry["completed"] = meta.get("completed") is True   # 未検証 dict → is True で正規化
            result.append(entry)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "set-description":
        import config
        config.reload_datasets()
        if args.dataset not in config.DATASETS:
            print(f"error: unknown dataset {args.dataset!r}", file=sys.stderr)
            return 1
        from llm_bridge import dataset_meta
        try:
            dataset_meta.patch_description(args.dataset, args.text)
        except (KeyError, RuntimeError, OSError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        return 0

    if args.cmd == "set-completed":
        import config
        config.reload_datasets()
        if args.dataset not in config.DATASETS:
            print(f"error: unknown dataset {args.dataset!r}", file=sys.stderr)
            return 1
        from llm_bridge import dataset_meta
        try:
            dataset_meta.patch_completed(args.dataset, not args.off)
        except (KeyError, RuntimeError, OSError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        return 0

    if args.cmd == "guard-write":
        from llm_bridge import guard_write
        return guard_write.main()

    if args.cmd == "engines":
        from llm_backend import preflight
        for st in preflight.check_all():
            mark = {"ok": "OK", "missing": "--", "unknown": "??", "n/a": "  "}
            auth = ", ".join(st.authed_providers) or st.auth_detail
            print(f"{st.engine_id:14s} "
                  f"prereq={mark.get(st.prereq_state, '')} "
                  f"install={mark.get(st.binary_state, '')} {st.version or '':10s} "
                  f"auth={mark.get(st.auth_state, '')} {auth}")
            if st.install:
                print(f"{'':14s}   install: {' '.join(st.install)}")
            # preflight は i18n を知らないので、文の組み立てはここで行う。
            from common.i18n import tr
            for key, params in st.notes:
                print(f"{'':14s}   {tr(key, **params)}")
            if st.note_key:
                print(f"{'':14s}   {tr(st.note_key)}")
        return 0

    if args.cmd == "doctor":
        import config
        config.reload_datasets()
        from llm_bridge import doctor
        return doctor.run(args.dataset, repair=args.repair, rescue=args.rescue,
                          cache=args.cache, log=args.log)

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
                from dataset_config import ensure_config, set_format
                if args.format is not None:
                    set_format(args.name, args.format)
                else:
                    ensure_config(args.name)   # 既存の myanalysis.toml は触らない
            except Exception as e:
                print(f"warning: could not set format in myanalysis.toml: {e}", file=sys.stderr)

        verb = "registered" if result["created"] else "updated"
        print(
            f"{verb} dataset {result['name']!r} for host "
            f"{result['host']!r}: {result['path']}"
        )

        # 新規登録を即 remote へ（best-effort、失敗・遅延は無視）
        try:
            import config_share
            config_share.try_sync()
        except Exception:
            pass

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
                    elif isinstance(r, str) and r.startswith("unreadable-session:"):
                        print(
                            "opened, but session.json is corrupt — tabs not "
                            "restored (file left untouched; see GUI log)",
                            file=sys.stderr,
                        )
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
        from llm_bridge import verbs

        def _print_table(title: str, table: dict[str, str]) -> None:
            print(title)
            for verb, usage in table.items():
                print(f"  {verb}  {usage}")

        if args.name is None:
            _print_table("window verbs:", verbs.WINDOW_VERBS)
            _print_table(
                "internal window verbs (GUI / meeting share; do not call unless "
                "the user asks):",
                verbs.INTERNAL_WINDOW_VERBS,
            )
            return 0
        _check_tab_name(args.name)
        _print_table("tab verbs (every tab):", verbs.COMMON_TAB_VERBS)
        _print_table("analysis tab verbs (tabs opened by add-tab):",
                     verbs.ANALYSIS_TAB_VERBS)
        _print_table("image-viewer tab verbs (tabs holding an image pane):",
                     verbs.IMAGE_VIEWER_VERBS)
        print("(analysis-specific verbs also registered at runtime — "
              "see analysis source)")
        print("Viewer tabs: `show`/`show-image slot=<path>` where <path> is "
              "left|right|top|bottom joined by '/' (e.g. top/left for a 2x2 grid); "
              "check current slots with `list-panes`.")
        return 0

    if args.cmd == "window":
        kwargs = _parse_kvs(args.kvs)
        cmd_id = _submit("window", None, args.verb, kwargs)
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
        cmd_id = _submit("tab", args.target, args.verb, kwargs)
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
        ds = _resolve_dataset(args)
        if ds is None:
            raise SystemExit("error: no dataset open; pass --dataset")
        _check_analysis_exists(ds, args.name)
        fields = _parse_kvs(args.kvs)
        annotations.submit(ds, args.name, args.kind, **fields)
        return 0

    if args.cmd == "clear-annotations":
        ds = _resolve_dataset(args)
        if ds is None:
            raise SystemExit("error: no dataset open; pass --dataset")
        _check_analysis_exists(ds, args.name)
        annotations.clear(ds, args.name, args.kind)
        return 0

    if args.cmd in ("draft-analysis", "apply-analysis", "recover-analysis"):
        ds = _resolve_dataset(args)
        if ds is None:
            raise SystemExit("error: no dataset open; pass --dataset")
        from llm_bridge import analysis_edit
        fn = {
            "draft-analysis": analysis_edit.draft_analysis,
            "apply-analysis": analysis_edit.apply_analysis,
            "recover-analysis": analysis_edit.recover_analysis,
        }[args.cmd]
        try:
            fn(ds, args.name)
        except (OSError, ValueError, KeyError, RuntimeError) as e:
            # 未捕捉の書き込み系例外（atomic_write_text/mkdir/analysis_out_dir(create=True)
            # の OSError 等）を agent-facing な SystemExit に正規化。
            # 対象がまさに同期マウントの書き込み失敗なので traceback を面に出さない。
            raise SystemExit(f"error: {e}")
        return 0

    if args.cmd in ("config-sync", "config-push", "config-pull"):
        import config_share
        fn = {"config-sync": config_share.sync,
              "config-push": config_share.push,
              "config-pull": config_share.pull}[args.cmd]
        kw = {"apply": not args.dry_run, "key": args.key}
        if args.cmd != "config-pull":
            kw["include_env"] = args.include_env
        try:
            result = fn(**kw)
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        for w in result.get("warnings", []):
            print(f"warning: {w}", file=sys.stderr)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
