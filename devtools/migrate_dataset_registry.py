"""旧 Python 登録簿（config.py の `DATASETS` リテラル）を JSON 登録簿へ移行する CLI。

    python -m devtools.migrate_dataset_registry --source <legacy-config.py> \
        [--output <registry.json>] [--dry-run]

旧ファイルは **import / exec / eval しない**。`ast.parse` + `ast.literal_eval` で
モジュール直下の単一 `DATASETS = {...}` / `DATASETS: ... = {...}` だけを読む。
`config`・同期機能・GUI を import せず、ネットワークにも接続しない（破損した既定
登録簿が残っていても復旧に使えるようにするため）。

既存の出力先は上書きしない: 同内容なら no-op（終了 0）、内容が異なれば失敗（終了 1）、
読み取り不能なら失敗（＝「未作成」と混同しない）。force オプションは無い。
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

import dataset_registry


def _load_legacy_datasets(source_path: Path) -> dict:
    """旧ソースから `DATASETS` の値だけを安全に取り出す（実行しない）。"""
    try:
        source = source_path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise dataset_registry.RegistryError(f"cannot read source: {e}") from e

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise dataset_registry.RegistryError(f"source is not valid Python: {e}") from e

    nodes = []
    for stmt in tree.body:
        if isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name) and stmt.target.id == "DATASETS":
                nodes.append(stmt)
        elif isinstance(stmt, ast.Assign):
            targets = stmt.targets
            if any(isinstance(t, ast.Name) and t.id == "DATASETS" for t in targets):
                if len(targets) != 1:
                    raise dataset_registry.RegistryError(
                        "DATASETS is part of a multi-target assignment"
                    )
                nodes.append(stmt)
    if not nodes:
        raise dataset_registry.RegistryError(
            f"no module-level DATASETS assignment found in {source_path.name}"
        )
    if len(nodes) > 1:
        raise dataset_registry.RegistryError(
            f"multiple module-level DATASETS assignments in {source_path.name}"
        )

    value = nodes[0].value
    if value is None:
        raise dataset_registry.RegistryError("DATASETS has no value")
    try:
        data = ast.literal_eval(value)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError) as e:
        raise dataset_registry.RegistryError(
            f"DATASETS is not a literal expression: {e}"
        ) from e

    return dataset_registry.validate_registry(data)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m devtools.migrate_dataset_registry",
        description="Convert a legacy config.py DATASETS literal into the JSON registry.",
    )
    parser.add_argument("--source", required=True, help="legacy config.py to read")
    parser.add_argument(
        "--output", default=None,
        help="destination registry JSON (default: dataset_registry.registry_path())",
    )
    parser.add_argument(
        "--dry-run", action="store_true", dest="dry_run", default=False,
        help="validate only; do not create the destination registry",
    )
    args = parser.parse_args(argv)

    source_path = Path(args.source)
    out_path = Path(args.output) if args.output else dataset_registry.registry_path()

    if args.output is not None and not out_path.parent.exists():
        print(
            f"error: output directory does not exist: {out_path.parent}",
            file=sys.stderr,
        )
        return 1

    try:
        datasets = _load_legacy_datasets(source_path)
    except dataset_registry.RegistryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    hosts = sum(len(per_host) for per_host in datasets.values())

    try:
        with dataset_registry.registry_transaction(config_path=out_path) as (fresh, writer):
            exists = out_path.exists()
            if exists and fresh == datasets:
                print(
                    f"already up to date: {len(datasets)} dataset(s), {hosts} host entry(ies)"
                )
                return 0
            if exists:
                print(
                    f"error: {out_path.name} already exists with different content "
                    "(not overwriting; resolve by hand)",
                    file=sys.stderr,
                )
                return 1
            # 未作成 = 書き込み予定がある分岐。dry-run も同じ検証を通す。
            dataset_registry.serialize_registry(datasets)
            if args.dry_run:
                print(
                    f"dry-run: would write {len(datasets)} dataset(s), "
                    f"{hosts} host entry(ies) to {out_path.name}"
                )
                return 0
            writer(datasets)
    except dataset_registry.RegistryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"error: cannot write {out_path.name}: {e}", file=sys.stderr)
        return 1

    print(f"migrated {len(datasets)} dataset(s), {hosts} host entry(ies) to {out_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
