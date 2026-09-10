"""Path configuration: dataset registry with per-host full paths.

各 dataset を「ホスト名 → その PC でのフルパス」で登録する。
同じ cloud drive フォルダが PC ごとに別ドライブにマウントされるため。

新しい dataset → DATASETS に追加。
新しい PC → 使う各 dataset にそのホスト名のエントリを追加(キーは大文字)。
"""
import ast
import contextlib
import json
import re
import socket
from pathlib import Path

from common.filelock import exclusive_lock
from common.mount_compat import install as _install_mount_compat
from common.paths import validate_identifier_name

_install_mount_compat()  # PIL/matplotlib の realpath(→WinError 1005) をマウント上で救う

# ホスト名は照合前に .upper() 済み。先頭は英数字 (非ASCII の文字も可)、以降は
# 英数字・'_'・'.'・'-'。パス区切り・空白・記号・制御文字は拒否する。
# \w は Unicode 既定で非ASCII 文字にマッチするため、ドイツ語等の PC 名も許可。
_HOST_RE = re.compile(r"^[^\W_][\w.\-]*$")

DATASETS: dict[str, dict[str, str]] = {
    "dataset_e": {
        "HOST_A": r"D:/measure/dataset_e",
    },
    "dataset_d": {
        "HOST_A": r"G:/測定/dataset_l/dataset_d",
        "HOST_B": r"H:\同期\測定\dataset_l\dataset_d",
    },
    "dataset_f": {
        "HOST_A": r"G:\測定\dataset_f",
    },
    "analysis_c": {
        "HOST_A": r"<repo>/data/analyses/dataset_c2",
    },
    "dataset_b": {
        "HOST_A": r"G:\測定\000000",
        "HOST_B": r"H:\同期\測定\000000",
    },
    "dataset_c": {
        "HOST_A": r"G:\測定\dataset_l\dataset_c3",
        "HOST_B": r"H:\同期\測定\dataset_l\dataset_c3",
    },
    "dataset_g": {
        "HOST_A": r"G:\測定\dataset_g",
    },
    "dataset_a": {
        "HOST_A": r"G:\測定\000000\example",
        "HOST_B": r"H:\同期\測定\000000\example",
    },
    "dataset_h": {
        "HOST_A": r"G:/測定/dataset_h",
    },
    "neuron_morph_demo": {
        "HOST_A": r"G:/測定/neuron_morph_demo",
    },
    "test": {
        "HOST_A": r"D:/work/run_1",
    },
    "dataset_j1": {
        "HOST_A": r"G:/測定/dataset_i1",
    },
    "dataset_j2": {
        "HOST_A": r"G:/測定/dataset_i2",
    },
    "dataset_j3": {
        "HOST_A": r"G:/測定/dataset_i3",
    },
    "dataset_j4": {
        "HOST_A": r"G:/測定/dataset_i4",
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


def _parse_registry(
    config_path: Path | None = None,
) -> tuple[ast.AST, dict, str | None, str]:
    """Parse config.py and extract the DATASETS registry.

    Returns (node, registry, annotation, source) where:
      - node: the ast.AnnAssign or ast.Assign node for DATASETS
      - registry: the parsed dict (via ast.literal_eval)
      - annotation: the unparsed type annotation string, or None
      - source: the raw file source text

    Raises TypeError if the parsed DATASETS value is not a dict.
    """
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
    if not isinstance(registry, dict):
        raise TypeError(
            f"DATASETS registry must be a dict, got {type(registry).__name__}"
        )

    annotation = None
    if isinstance(node, ast.AnnAssign):
        annotation = ast.unparse(node.annotation)

    return node, registry, annotation, source


def reload_datasets(config_path: Path | None = None) -> None:
    """Re-read DATASETS from config.py and update the module-level dict in place.

    In-place clear/update preserves dict identity — code that imported
    ``from config import DATASETS`` or holds a reference to the dict object
    sees the refreshed data without re-importing.
    """
    _, registry, _, _ = _parse_registry(config_path)
    # _parse_registry guarantees registry is a dict (TypeError otherwise).
    DATASETS.clear()
    DATASETS.update(registry)


def _config_lock_path(config_path: Path) -> Path:
    return config_path.with_name(config_path.name + ".lock")   # 例: config.py.lock


def _replace_datasets_block(node, source: str, block: str, config_path: Path) -> None:
    """DATASETS ブロックを行スライスで差し替え tmp→replace（パース・ロックなし）。
    改行コード(CRLF/LF)を保存。"""
    newline = "\r\n" if "\r\n" in source else "\n"
    lines = source.splitlines(keepends=True)
    new_lines = [ln + newline for ln in block.splitlines()]
    # ast line numbers are 1-based; slice is 0-based.
    lines[node.lineno - 1 : node.end_lineno] = new_lines
    tmp = config_path.with_suffix(".py.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write("".join(lines))
    tmp.replace(config_path)


@contextlib.contextmanager
def registry_transaction(*, config_path: Path | None = None):
    """config.py のロックを取り、(fresh_registry, writer) を yield する。

    呼び出し側は yield された **その時点の最新** registry（fresh_registry）を見て新
    registry を組み立て、writer(new_registry) を呼ぶと原子的に書き戻す（writer 未呼出なら
    書かない）。fresh_registry の読み出し〜writer の書き込みまで同一ロックを保持するので、
    make_bundle 時点の stale snapshot を丸ごと書き戻して他プロセスの追加を消す lost update
    を防ぐ。ネットワーク I/O はこのブロックの外で行うこと。in-memory DATASETS は変更しない
    （呼び出し側が reload_datasets() を呼ぶ）。
    """
    if config_path is None:
        config_path = Path(__file__)
    with exclusive_lock(_config_lock_path(config_path)):
        node, registry, annotation, source = _parse_registry(config_path)

        def writer(new_registry: dict) -> None:
            block = _serialize_datasets(new_registry, annotation)
            _replace_datasets_block(node, source, block, config_path)

        yield registry, writer


def write_registry(registry: dict, *, config_path: Path | None = None) -> None:
    """完成した registry を丸ごと安全に書き戻す（ロック内・原子的）。in-memory DATASETS は不変。
    呼び出し側が reload_datasets() を呼ぶ。CRLF/LF・raw-string/json フォールバック・無関係行
    不変を保つ。※全置換セマンティクスなので、並行追加を保ちたい RMW では
    registry_transaction を使うこと。"""
    with registry_transaction(config_path=config_path) as (_fresh, writer):
        writer(registry)


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

    created = False
    with registry_transaction(config_path=config_path) as (registry, writer):
        created = name not in registry
        registry.setdefault(name, {})[host] = path
        writer(registry)

    return {"name": name, "host": host, "path": path, "created": created}
