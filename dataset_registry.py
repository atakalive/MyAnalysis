"""Dataset registry storage — Git 管理外の JSON に登録データを保存する（Issue #95）。

登録簿の実体は `repo_root() / "datasets.local.json"`（gitignored）。形式は

    {"<dataset 名>": {"<HOST>": "<その PC でのフルパス>"}}

の 2 段 dict のみで、外側ラッパーや schema_version は持たない。

このモジュールは **保存処理だけ**を持ち、`config`・GUI・同期モジュールには依存しない
（循環 import を作らないため）。名前・ホスト・絶対パスの検証は登録 API
（`config.register_dataset`）の責務で、ここでは型だけを検証する。

import 自体は I/O を行わない（登録簿・ロック・ディレクトリを作らない）。
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

from common.filelock import exclusive_lock
from common.paths import atomic_write_text, repo_root

Registry = dict[str, dict[str, str]]

REGISTRY_NAME = "datasets.local.json"


class RegistryError(ValueError):
    """登録簿の読み書き・検証の失敗（不正 JSON・型不正・エンコード不能・I/O 失敗）。"""


def registry_path() -> Path:
    """既定の登録簿パス。ファイルは作らない（cwd・HOME・env に依存しない）。"""
    return repo_root() / REGISTRY_NAME


def _target(config_path: Path | None) -> Path:
    """実際に読み書きするパス。明示指定は `.json` のみ受理（I/O 前に判定）。"""
    if config_path is None:
        return registry_path()
    p = Path(config_path)
    if p.suffix.lower() != ".json":
        raise RegistryError(
            f"registry path must be a .json file, got {p.name!r} "
            "(the registry is no longer a Python source file)"
        )
    return p


def validate_registry(value: object) -> Registry:
    """`{str: {str: str}}` であることを検証し、そのまま返す。違反は RegistryError。"""
    if not isinstance(value, dict):
        raise RegistryError(
            f"registry must be a dict, got {type(value).__name__}"
        )
    for ds_name, per_host in value.items():
        if not isinstance(ds_name, str):
            raise RegistryError(
                f"dataset key must be a str, got {type(ds_name).__name__}"
            )
        if not isinstance(per_host, dict):
            raise RegistryError(
                f"dataset {ds_name!r} must map to a dict, "
                f"got {type(per_host).__name__}"
            )
        for host, path in per_host.items():
            if not isinstance(host, str):
                raise RegistryError(
                    f"host key of dataset {ds_name!r} must be a str, "
                    f"got {type(host).__name__}"
                )
            if not isinstance(path, str):
                raise RegistryError(
                    f"path of dataset {ds_name!r} must be a str, "
                    f"got {type(path).__name__}"
                )
    return value  # type: ignore[return-value]


def serialize_registry(registry: Registry) -> str:
    """検証済み JSON テキストを返す（副作用なし・ファイルに触れない）。

    型検証 → `json.dumps(..., ensure_ascii=False, indent=2)` → 全文の UTF-8
    エンコード可否検証、の順。孤立サロゲート（例 `"\\ud800"`）は json は通るが
    UTF-8 エンコードで落ちるため、ここで弾いて `atomic_write_text` に到達させない
    （fragile FS の in-place write は先に truncate するので、到達後の失敗は
    既存ファイルを空にし得る）。
    """
    validate_registry(registry)
    text = json.dumps(registry, ensure_ascii=False, indent=2) + "\n"
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as e:
        raise RegistryError(f"registry is not encodable as UTF-8: {e}") from e
    return text


def read_registry(config_path: Path | None = None) -> Registry:
    """登録簿を読む（毎回ディスクを読む・ロックは取らない）。

    未作成（`FileNotFoundError`）のときだけ新しい空辞書 `{}` を返す。存在する空
    ファイル・空白のみ・不正 JSON・不正 UTF-8・型不正・その他の `OSError` は
    `RegistryError` とし、空辞書へ置き換えない（破損を「未設定」と混同しない）。
    """
    target = _target(config_path)
    try:
        raw = target.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except UnicodeDecodeError as e:
        raise RegistryError(f"{target.name}: invalid UTF-8: {e}") from e
    except OSError as e:
        raise RegistryError(f"{target.name}: cannot read: {e}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RegistryError(f"{target.name}: invalid JSON: {e}") from e
    try:
        return validate_registry(data)
    except RegistryError as e:
        raise RegistryError(f"{target.name}: {e}") from e


@contextlib.contextmanager
def registry_transaction(*, config_path: Path | None = None):
    """登録簿のロックを取り、`(fresh_registry, writer)` を yield する。

    呼び出し側は yield された **その時点の最新** registry を見て新しい registry を
    組み立て、`writer(new_registry)` で書き戻す（writer 未呼出なら書かない）。読み出し
    〜書き込みまで同一ロックを保持するので、stale snapshot の書き戻しによる lost update
    を防ぐ。ネットワーク I/O はこのブロックの外で行うこと。in-memory の
    `config.DATASETS` は変更しない（呼び出し側が `reload_datasets()` を呼ぶ）。

    **非再入（non-reentrant）**: `exclusive_lock` は毎回新しい `open()` でロックファイルを
    開くため、同一プロセスでもこのブロック内から `write_registry` や別の
    `registry_transaction` をネスト呼び出しすると別ハンドルで競合する（POSIX の
    `fcntl.flock` は外側の解放を無期限に待ってブロックし得る／Windows の
    `msvcrt.locking` は 3 回リトライ後に `OSError`）。ブロック内では yield された
    `writer` だけを使い、ネスト呼び出しをしないこと。
    """
    target = _target(config_path)
    lock_name = target.with_name(target.name + ".lock")
    with exclusive_lock(lock_name):
        registry = read_registry(target)

        def writer(new_registry: Registry) -> None:
            text = serialize_registry(new_registry)
            atomic_write_text(target, text, encoding="utf-8", newline="\n")

        yield registry, writer


def write_registry(registry: Registry, *, config_path: Path | None = None) -> None:
    """完成した registry を丸ごと書き戻す（ロック内・全置換）。

    in-memory の `config.DATASETS` は不変（呼び出し側が `reload_datasets()` を呼ぶ）。
    ※全置換セマンティクスなので、並行追加を保ちたい RMW では
    `registry_transaction` を使うこと。
    """
    with registry_transaction(config_path=config_path) as (_fresh, writer):
        writer(registry)
