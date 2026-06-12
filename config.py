"""Path configuration: dataset registry with per-host full paths.

各 dataset を「ホスト名 → その PC でのフルパス」で登録する。
同じ cloud drive フォルダが PC ごとに別ドライブにマウントされるため。

新しい dataset → DATASETS に追加。
新しい PC → 使う各 dataset にそのホスト名のエントリを追加(キーは大文字)。
"""
import ast
import json
import re
import socket
from pathlib import Path

from common.paths import validate_identifier_name

# ホスト名は照合前に .upper() 済み。先頭は英数字 (非ASCII の文字も可)、以降は
# 英数字・'_'・'.'・'-'。パス区切り・空白・記号・制御文字は拒否する。
# \w は Unicode 既定で非ASCII 文字にマッチするため、ドイツ語等の PC 名も許可。
_HOST_RE = re.compile(r"^[^\W_][\w.\-]*$")

DATASETS: dict[str, dict[str, str]] = {
    "dataset_a": {
        "HOST_A": r"G:\同期\測定\000000\example",
        "HOST_B": r"H:\同期\測定\000000\example",
    },
    "dataset_b": {
        "HOST_A": r"G:\同期\測定\000000",
        "HOST_B": r"H:\同期\測定\000000",
    },
    "dataset_c": {
        "HOST_A": r"G:\同期\測定\dataset_l\dataset_c3",
    },
    "analysis_c": {
        "HOST_A": r"<repo>/data/analyses/dataset_c2",
    },
    "dataset_d": {
        "HOST_A": r"G:/同期/測定/dataset_l/dataset_d",
    },
}


def get_dataset_dir(name: str) -> Path:
    try:
        per_host = DATASETS[name]
    except KeyError:
        known = ", ".join(DATASETS) or "(none)"
        raise KeyError(
            f"Unknown dataset {name!r}. "
            f"Add it to DATASETS in config.py. Known datasets: {known}"
        )
    host = socket.gethostname().upper()
    try:
        return Path(per_host[host])
    except KeyError:
        known = ", ".join(per_host) or "(none)"
        raise RuntimeError(
            f"Dataset {name!r} has no path for hostname {host!r}. "
            f"Add it to DATASETS[{name!r}] in config.py. Known hosts: {known}"
        )


def _serialize_path_value(path: str) -> str:
    """Serialize a path string as a Python literal.

    raw string `r"..."` when safe (no `"`, no trailing `\\`, no control chars);
    otherwise a `json.dumps` double-quoted string (always syntactically valid).
    """
    has_control = any(ord(c) < 32 or ord(c) == 127 for c in path)
    if '"' not in path and not path.endswith("\\") and not has_control:
        return f'r"{path}"'
    return json.dumps(path, ensure_ascii=False)


def _serialize_datasets(registry: dict, annotation: str | None) -> str:
    """Re-serialize the DATASETS registry to a Python literal source block.

    Keys (dataset + host names) are emitted via json.dumps; path values follow
    the raw-string-or-json rule in _serialize_path_value.
    """
    lines: list[str] = []
    if annotation is not None:
        lines.append(f"DATASETS: {annotation} = {{")
    else:
        lines.append("DATASETS = {")
    for ds_name, per_host in registry.items():
        lines.append(f"    {json.dumps(ds_name, ensure_ascii=False)}: {{")
        for host, path in per_host.items():
            key = json.dumps(host, ensure_ascii=False)
            lines.append(f"        {key}: {_serialize_path_value(path)},")
        lines.append("    },")
    lines.append("}")
    return "\n".join(lines)


def register_dataset(
    name: str,
    path: str,
    host: str | None = None,
    *,
    config_path: Path | None = None,
) -> dict[str, str | bool]:
    """Register a dataset in config.py (file write only, no in-memory mutation).

    Returns {"name": str, "host": str, "path": str, "created": bool}.
    "created" is True if the dataset name was new, False if merged/updated.
    """
    from pathlib import PurePosixPath, PureWindowsPath

    validate_identifier_name(name, check_reserved=False)

    host = (host or socket.gethostname()).upper()
    if not _HOST_RE.fullmatch(host):
        raise ValueError(f"invalid hostname: {host!r}")

    if not (PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()):
        raise ValueError(f"path must be absolute: {path!r}")

    if config_path is None:
        config_path = Path(__file__)

    with open(config_path, encoding="utf-8", newline="") as f:
        source = f.read()
    tree = ast.parse(source)

    node = None
    for stmt in tree.body:
        if isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name) and stmt.target.id == "DATASETS":
                node = stmt
                break
        elif isinstance(stmt, ast.Assign):
            if any(
                isinstance(t, ast.Name) and t.id == "DATASETS" for t in stmt.targets
            ):
                node = stmt
                break
    if node is None:
        raise ValueError(f"DATASETS assignment not found in {config_path}")

    registry = ast.literal_eval(ast.get_source_segment(source, node.value))

    annotation = None
    if isinstance(node, ast.AnnAssign):
        annotation = ast.unparse(node.annotation)

    created = name not in registry
    registry.setdefault(name, {})[host] = path

    block = _serialize_datasets(registry, annotation)

    newline = "\r\n" if "\r\n" in source else "\n"
    lines = source.splitlines(keepends=True)
    new_lines = [l + newline for l in block.splitlines()]
    # ast line numbers are 1-based; slice is 0-based.
    lines[node.lineno - 1 : node.end_lineno] = new_lines

    tmp = config_path.with_suffix(".py.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write("".join(lines))
    tmp.replace(config_path)

    return {"name": name, "host": host, "path": path, "created": created}
