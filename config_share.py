"""Cloudflare R2 経由で repo 側 config を push/pull する（任意・best-effort）。

R2 が運ぶのは「同期ドライブに乗らない repo 側 config」だけ＝ DATASETS（config.py）
＋ gitignore な repo 直下の portable files（models.toml / llm_backend/config.toml）と
（任意）.env。データセット側の analysis.py / _work / state / batch / myanalysis.toml は
同期ドライブで sync 済みなので bundle に含めない。

best-effort 原則: 自動経路（try_sync）はクレデンシャル未設定・boto3 未導入・ネット不通・
バケットエラー・remote 破損のいずれでも例外で起動を止めず、長時間ブロックもしない
（boto3 は short timeout＋リトライ無効）。明示 CLI（sync/push/pull）はエラーを表に出す。

boto3 はモジュール先頭では import せず _client() で遅延 import する。
"""
import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import config
from common.env import load_env
from common.paths import repo_root

SCHEMA_VERSION = 1
DEFAULT_PREFIX = "config/"
BUNDLE_NAME = "bundle.json"        # key = prefix + BUNDLE_NAME
PORTABLE_FILES = ["models.toml", "llm_backend/config.toml"]  # repo_root() 基準。.env は既定で含めない。
_ALLOWED_FILES = set(PORTABLE_FILES) | {".env"}  # remote の files で許可する key の whitelist
PUT_RETRIES = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(s: str | None) -> "datetime | None":
    """None/不正 -> None（最古扱い）。naive -> UTC とみなし aware 化（比較の TypeError 回避）。"""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iso_from_mtime(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


def _log_debug(msg: str) -> None:
    if os.environ.get("R2_DEBUG"):
        print(f"[config_share] {msg}", file=sys.stderr)


def _tie_break(a: str, b: str) -> bool:
    """同時刻衝突の決定的勝者選択。a を採用するなら True（a >= b の文字列比較）。
    全機が同じ (a,b) で同じ結論を出すので収束する。"""
    return a >= b


def _read_file_state(p: Path) -> tuple[str, str | None, float | None]:
    """ファイル状態を3値で返す（『未存在』と『存在するが読めない』を区別する）:
      - ('missing', None, None)    : ファイルが無い。
      - ('unreadable', None, None) : 存在するが読めない（権限・非UTF8 など）。
      - ('text', <str>, <mtime>)   : UTF-8 で読めた（content と st_mtime を同時取得）。
    read_text と stat を同一 try 内で取得し、read 後に別途 stat する TOCTOU
    （その隙にファイルが消えると stat が FileNotFoundError でクラッシュ）を排除する。"""
    try:
        text = p.read_text(encoding="utf-8")
        mtime = p.stat().st_mtime
        return ("text", text, mtime)
    except FileNotFoundError:
        return ("missing", None, None)
    except (OSError, UnicodeDecodeError):
        return ("unreadable", None, None)


def _collect_portable_files(*, include_env: bool, warnings: list) -> tuple[dict, dict, set]:
    """portable files を **その時点で fresh に** 読む。戻り: (files, file_meta, unreadable)。
      - 'missing'    -> どれにも入れない（remote-only なら新規作成の対象になり得る）。
      - 'text'       -> files[rel]=text, file_meta[rel]=_iso_from_mtime(mtime)。
      - 'unreadable' -> unreadable に rel を入れ warning。files には入れない。"""
    files, file_meta, unreadable = {}, {}, set()
    rels = list(PORTABLE_FILES) + ([".env"] if include_env else [])
    for rel in rels:
        kind, text, mtime = _read_file_state(repo_root() / rel)
        if kind == "missing":
            continue
        if kind == "unreadable":
            unreadable.add(rel)
            msg = f"読込不能のため {rel!r} を同期対象から除外（ローカルを保持）"
            if msg not in warnings:
                warnings.append(msg)
            continue
        files[rel] = text
        file_meta[rel] = _iso_from_mtime(mtime)
    return files, file_meta, unreadable


def _load_creds(required: bool = True) -> dict | None:
    """common.env.load_env() で .env 反映後、os.environ から R2_* を読む。
    必須キー: R2_ENDPOINT_URL / R2_BUCKET / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY
    任意: R2_PREFIX（既定 DEFAULT_PREFIX）。prefix は末尾 '/' を正規化。
    required=True: 欠けたら raise。required=False: 1つでも欠ければ None。"""
    load_env()
    endpoint = os.environ.get("R2_ENDPOINT_URL")
    bucket = os.environ.get("R2_BUCKET")
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    if not all((endpoint, bucket, access_key, secret_key)):
        if required:
            raise RuntimeError(
                "R2 が未設定です。.env に R2_ENDPOINT_URL / R2_BUCKET / "
                "R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY を設定してください。"
            )
        return None
    prefix = os.environ.get("R2_PREFIX", DEFAULT_PREFIX)
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return {
        "endpoint": endpoint,
        "bucket": bucket,
        "access_key": access_key,
        "secret_key": secret_key,
        "prefix": prefix,
    }


def is_configured() -> bool:
    return _load_creds(required=False) is not None     # ネットワークは叩かない


def _client(creds: dict):
    """boto3 を遅延 import。R2 固有: region_name='auto' と SigV4。
    起動/CLI をブロックしないよう connect/read timeout を短く、リトライ無効化する。
    boto3 未導入時は明示CLIでは案内して raise、自動経路は try_sync が握りつぶす。"""
    try:
        import boto3
        from botocore.config import Config
    except ImportError as e:
        raise RuntimeError(
            "boto3 が未導入です。R2 同期を使うには `pip install boto3` してください。"
        ) from e
    return boto3.client(
        "s3",
        endpoint_url=creds["endpoint"],
        aws_access_key_id=creds["access_key"],
        aws_secret_access_key=creds["secret_key"],
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            connect_timeout=2,
            read_timeout=3,
            retries={"max_attempts": 1, "mode": "standard"},
        ),
    )


def _err_code(exc) -> str | None:
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        return resp.get("Error", {}).get("Code")
    return None


_NOT_FOUND = {"NoSuchKey", "NoSuchBucket", "404", "NotFound"}
_PRECONDITION = {"PreconditionFailed", "412"}


def _state_path() -> Path:
    return Path.home() / ".myanalysis" / "config_share_state.json"


def _local_state() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"entry_meta": {}, "etag": None}
    if not isinstance(data, dict):
        return {"entry_meta": {}, "etag": None}
    data.setdefault("entry_meta", {})
    data.setdefault("etag", None)
    return data


def _save_local_state(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def make_bundle(*, config_path: Path | None = None, include_env: bool = False,
                warnings: list | None = None) -> dict:
    """ローカルの寄与を bundle dict にする（remote とは未マージ）。"""
    warnings = [] if warnings is None else warnings
    if config_path is None:
        config_path = Path(config.__file__)      # 既定経路＝実 config.py を読む（明示正規化）

    datasets = config._parse_registry(config_path)[1]

    state = _local_state()
    entry_meta = {}
    for ds, per_host in datasets.items():
        for host in per_host:
            key = f"{ds}/{host}"
            if key in state["entry_meta"]:
                entry_meta[key] = state["entry_meta"][key]

    files, file_meta, _unreadable = _collect_portable_files(
        include_env=include_env, warnings=warnings
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "updated_by": socket.gethostname().upper(),
        "updated_at": _now_iso(),
        "datasets": datasets,
        "entry_meta": entry_meta,
        "files": files,
        "file_meta": file_meta,
    }


def _validate_bundle(bundle) -> bool:
    """remote を信頼しないための厳密型検証。NG なら同期で適用しない。"""
    if not isinstance(bundle, dict):
        return False
    if not isinstance(bundle.get("schema_version"), int):
        return False
    datasets = bundle.get("datasets")
    if not isinstance(datasets, dict):
        return False
    for ds, per_host in datasets.items():
        if not isinstance(ds, str) or not isinstance(per_host, dict):
            return False
        for host, path in per_host.items():
            if not isinstance(host, str) or not isinstance(path, str):
                return False
    for opt in ("entry_meta", "files", "file_meta"):
        val = bundle.get(opt)
        if val is None:
            continue
        if not isinstance(val, dict):
            return False
        for k, v in val.items():
            if not isinstance(k, str) or not isinstance(v, str):
                return False
    return True


def _get_remote(client, bucket: str, key: str) -> tuple[dict | None, str | None, bool]:
    """戻り: (bundle, etag, unusable)。
      - NoSuchKey/404 -> (None, None, False)  # 空 remote（初回 push 可）
      - 取得成功 & schema_version <= SCHEMA_VERSION & _validate_bundle True
            -> (bundle, etag, False)
      - 取得成功だが schema_version > SCHEMA_VERSION か _validate_bundle False
            -> (None, etag, True)  # read-only
    """
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if _err_code(exc) in _NOT_FOUND:
            return (None, None, False)
        raise
    etag = resp.get("ETag")
    body = resp["Body"].read()
    try:
        bundle = json.loads(body)
    except (ValueError, TypeError):
        return (None, etag, True)
    if not _validate_bundle(bundle):
        return (None, etag, True)
    if bundle.get("schema_version", 0) > SCHEMA_VERSION:
        return (None, etag, True)
    return (bundle, etag, False)


def _safe_target(rel: str, root: Path) -> Path | None:
    """remote files の相対キー rel を安全な書き込み先 Path に変換。危険なら None。
    .env は .env.pulled へ固定変換（実 .env は自動上書きしない）。"""
    if rel == ".env":
        name = ".env.pulled"
    else:
        name = rel
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts:        # absolute / 親参照を拒否
        return None
    root = root.resolve()
    target = (root / name).resolve()
    try:
        target.relative_to(root)                  # repo_root 配下に限定
    except ValueError:
        return None
    return target


def _expand_remote(remote: dict | None, warnings: list) -> tuple[dict, dict, dict, dict, list]:
    """remote（None=空 remote）を (datasets, entry_meta, files, file_meta, dropped) に展開。
    files/file_meta は _ALLOWED_FILES の whitelist 内のみ採用し、許可外キーは dropped に集め
    warning する。"""
    remote = remote or {}
    datasets = remote.get("datasets") or {}
    entry_meta = remote.get("entry_meta") or {}
    raw_files = remote.get("files") or {}
    raw_file_meta = remote.get("file_meta") or {}
    files, file_meta, dropped = {}, {}, []
    for k, v in raw_files.items():
        if k in _ALLOWED_FILES:
            files[k] = v
            if k in raw_file_meta:
                file_meta[k] = raw_file_meta[k]
        else:
            dropped.append(k)
            msg = f"remote files の許可外キーを無視: {k!r}"
            if msg not in warnings:
                warnings.append(msg)
    return datasets, entry_meta, files, file_meta, dropped


def _merge_datasets(local_reg, local_meta, remote_reg, remote_meta, self_host, now_iso):
    """戻り: (merged_reg, merged_meta)。(dataset,HOST) の union・非破壊（削除しない）。
    各キーの path と meta を決め、meta が None のキーは merged_meta に入れない（omit）。"""
    merged_reg: dict = {}
    merged_meta: dict = {}

    all_ds = set(local_reg) | set(remote_reg)
    for ds in all_ds:
        local_hosts = local_reg.get(ds, {})
        remote_hosts = remote_reg.get(ds, {})
        per_host: dict = {}
        for host in set(local_hosts) | set(remote_hosts):
            key = f"{ds}/{host}"
            in_local = host in local_hosts
            in_remote = host in remote_hosts
            if in_local and not in_remote:
                per_host[host] = local_hosts[host]
                meta = local_meta.get(key) or now_iso
            elif in_remote and not in_local:
                per_host[host] = remote_hosts[host]
                meta = remote_meta.get(key)
            else:
                lp, rp = local_hosts[host], remote_hosts[host]
                if lp == rp:
                    per_host[host] = lp
                    meta = remote_meta.get(key) or local_meta.get(key)
                elif host == self_host:
                    per_host[host] = lp
                    meta = now_iso
                else:
                    lt = _parse_iso(local_meta.get(key))
                    rt = _parse_iso(remote_meta.get(key))
                    if lt is None and rt is None:
                        per_host[host] = rp
                        meta = remote_meta.get(key)
                    elif lt is None:
                        per_host[host] = rp
                        meta = remote_meta.get(key)
                    elif rt is None:
                        per_host[host] = lp
                        meta = local_meta.get(key)
                    elif lt > rt:
                        per_host[host] = lp
                        meta = local_meta.get(key)
                    elif rt > lt:
                        per_host[host] = rp
                        meta = remote_meta.get(key)
                    else:  # 同時刻
                        if _tie_break(lp, rp):
                            per_host[host] = lp
                        else:
                            per_host[host] = rp
                        meta = local_meta.get(key) or remote_meta.get(key)
            if meta is not None:
                merged_meta[key] = meta
        merged_reg[ds] = per_host
    return merged_reg, merged_meta


def _merge_files(local_files, local_file_meta, remote_files, remote_file_meta, now_iso):
    """戻り: (merged_files, merged_file_meta, writes)。
    writes = {rel: (text, remote_meta_iso_or_None)} はローカルへ書くべきファイル。
    merged_file_meta も None のキーは omit。"""
    merged_files: dict = {}
    merged_file_meta: dict = {}
    writes: dict = {}

    for rel in set(local_files) | set(remote_files):
        in_local = rel in local_files
        in_remote = rel in remote_files
        if in_local and not in_remote:
            merged_files[rel] = local_files[rel]
            meta = local_file_meta.get(rel) or now_iso
        elif in_remote and not in_local:
            merged_files[rel] = remote_files[rel]
            meta = remote_file_meta.get(rel)
            writes[rel] = (remote_files[rel], remote_file_meta.get(rel))
        elif local_files[rel] == remote_files[rel]:
            merged_files[rel] = local_files[rel]
            meta = remote_file_meta.get(rel) or local_file_meta.get(rel)
        else:
            lt = _parse_iso(local_file_meta.get(rel))
            rt = _parse_iso(remote_file_meta.get(rel))
            if rt is not None and (lt is None or rt > lt):
                merged_files[rel] = remote_files[rel]
                meta = remote_file_meta.get(rel)
                writes[rel] = (remote_files[rel], remote_file_meta.get(rel))
            elif lt is not None and (rt is None or lt > rt):
                merged_files[rel] = local_files[rel]
                meta = local_file_meta.get(rel)   # ★実 st_mtime（now ではない）
            else:  # 同時刻（双方 None も含む）
                if _tie_break(local_files[rel], remote_files[rel]):
                    merged_files[rel] = local_files[rel]
                    meta = local_file_meta.get(rel) or remote_file_meta.get(rel)
                else:
                    merged_files[rel] = remote_files[rel]
                    meta = remote_file_meta.get(rel) or local_file_meta.get(rel)
                    writes[rel] = (remote_files[rel], remote_file_meta.get(rel))
        if meta is not None:
            merged_file_meta[rel] = meta
    return merged_files, merged_file_meta, writes


def sync(*, direction: str = "both", apply: bool = True,
         include_env: bool = False, key: str | None = None,
         config_path: Path | None = None) -> dict:
    """pull→merge→push の read-modify-write。
    direction: "both"（既定/自動）/ "pull"（remote->local のみ）/ "push"（マージ後 remote のみ）。
    apply=False（dry-run）= どちらにも書かず差分サマリを返す。"""
    warnings: list = []
    creds = _load_creds(required=True)
    bucket = creds["bucket"]
    object_key = key or (creds["prefix"] + BUNDLE_NAME)
    client = _client(creds)
    self_host = socket.gethostname().upper()
    now = _now_iso()
    state = _local_state()

    # sidecar entry_meta（datasets の LWW 用）を取得するために make_bundle を使う。
    local = make_bundle(config_path=config_path, include_env=include_env, warnings=warnings)

    remote, etag, unusable = _get_remote(client, bucket, object_key)
    if unusable:
        warnings.append("remote bundle が未知スキーマ/不正のため read-only フォールバック")
        if apply:
            state["etag"] = etag
            _save_local_state(state)
        return {
            "remote_present": True, "unusable_remote": True, "pushed": False,
            "etag": etag, "datasets_total": len(local["datasets"]),
            "files_total": len(local["files"]),
            "planned": {"write_datasets": False, "write_files": [], "would_push": False},
            "warnings": warnings, "dry_run": (not apply),
        }

    pushed = False
    would_write_datasets = False
    would_push = False
    planned_write_files: list = []
    merged_reg: dict = local["datasets"]
    merged_files: dict = {}

    for _attempt in range(PUT_RETRIES):
        (remote_datasets, remote_entry_meta, remote_files,
         remote_file_meta, dropped_file_keys) = _expand_remote(remote, warnings)

        local_files, local_file_meta, unreadable_files = _collect_portable_files(
            include_env=include_env, warnings=warnings
        )

        merged_files, merged_file_meta, writes = _merge_files(
            local_files, local_file_meta, remote_files, remote_file_meta, now
        )

        wrote_local = False
        with config.registry_transaction(config_path=config_path) as (fresh_reg, writer):
            merged_reg, merged_meta = _merge_datasets(
                fresh_reg, local["entry_meta"], remote_datasets,
                remote_entry_meta, self_host, now,
            )
            would_write_datasets = direction in ("both", "pull") and merged_reg != fresh_reg
            if apply and would_write_datasets:
                writer(merged_reg)
                wrote_local = True
        if wrote_local:
            config.reload_datasets(config_path=config_path)

        planned_write_files = list(writes) if direction in ("both", "pull") else []

        if apply and direction in ("both", "pull"):
            for rel, (text, rmeta) in writes.items():
                if rel != ".env" and rel in unreadable_files:
                    warnings.append(
                        f"{rel!r} は読込不能のため remote で上書きしない（ローカルを保持）"
                    )
                    continue
                target = _safe_target(rel, repo_root())
                if target is None:
                    warnings.append(f"安全でない書き込み先を拒否: {rel!r}")
                    continue
                if rel != ".env":
                    kind, cur, _cur_mtime = _read_file_state(repo_root() / rel)
                    if kind == "unreadable":
                        warnings.append(
                            f"{rel!r} が読込不能のため remote で上書きしない（ローカルを保持）"
                        )
                        continue
                    if cur != local_files.get(rel):
                        warnings.append(
                            f"ローカルが直前に変更されたため {rel!r} の反映を skip"
                            "（次回 sync で再マージ）"
                        )
                        continue
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".tmp")
                tmp.write_text(text, encoding="utf-8")
                tmp.replace(target)
                rdt = _parse_iso(rmeta)
                if rdt is not None:
                    now_epoch = _parse_iso(now).timestamp()
                    os.utime(target, (now_epoch, rdt.timestamp()))

        if apply and direction in ("both", "pull") and wrote_local:
            state["entry_meta"] = merged_meta

        would_push = direction in ("both", "push") and (
            merged_reg != remote_datasets
            or merged_files != remote_files
            or bool(dropped_file_keys)
        )
        need_push = apply and would_push
        if not need_push:
            break

        # put 直前ガード（local->remote stale upload 防止）: ループ冒頭の fresh read 以降、
        # config.py transaction やローカル write を挟む間にユーザーが portable file を編集する
        # と、merged_files（= put 内容）に古い snapshot が残り得る。put 直前にもう一度読み、
        # 我々が writes で書いたファイル以外で loop 冒頭と差があれば、再ループして fresh に
        # 再マージしてから push する（remote->local 書き込みと対称の RMW ガード）。
        recheck_files, _rc_meta, _rc_unread = _collect_portable_files(
            include_env=include_env, warnings=warnings
        )
        external_change = any(
            recheck_files.get(rel) != local_files.get(rel)
            for rel in (set(local_files) | set(recheck_files))
            if rel not in writes
        )
        if external_change:
            _log_debug("portable files changed during sync; re-merging before push")
            continue

        put_bundle = {
            "schema_version": SCHEMA_VERSION,
            "updated_by": self_host,
            "updated_at": now,
            "datasets": merged_reg,
            "entry_meta": merged_meta,
            "files": merged_files,
            "file_meta": merged_file_meta,
        }
        body = json.dumps(put_bundle, ensure_ascii=False).encode("utf-8")
        put_kwargs = {"Bucket": bucket, "Key": object_key, "Body": body,
                      "ContentType": "application/json"}
        if etag is None:
            put_kwargs["IfNoneMatch"] = "*"
        else:
            put_kwargs["IfMatch"] = etag
        try:
            resp = client.put_object(**put_kwargs)
            etag = resp.get("ETag")
            pushed = True
            break
        except Exception as exc:
            if _err_code(exc) in _PRECONDITION:
                remote, etag, unusable = _get_remote(client, bucket, object_key)
                if unusable:
                    warnings.append(
                        "remote bundle が未知スキーマ/不正のため read-only フォールバック"
                    )
                    break
                continue
            raise
    else:
        warnings.append("PUT リトライ上限に達したため push を諦めました")

    if apply:
        state["etag"] = etag
        _save_local_state(state)

    return {
        "remote_present": remote is not None,
        "unusable_remote": False,
        "pushed": pushed,
        "etag": etag,
        "datasets_total": len(merged_reg),
        "files_total": len(merged_files),
        "planned": {
            "write_datasets": would_write_datasets,
            "write_files": planned_write_files,
            "would_push": would_push,
        },
        "warnings": warnings,
        "dry_run": (not apply),
    }


def push(**kw) -> dict:
    return sync(direction="push", **kw)


def pull(**kw) -> dict:
    return sync(direction="pull", **kw)


def try_sync(*, config_path: Path | None = None) -> dict | None:
    """繋がるときだけ双方向収束。起動も登録も絶対に止めない。戻り値は無視してよい。"""
    try:
        load_env()
        if os.environ.get("R2_AUTOSYNC", "1") == "0":
            return None
        if not is_configured():
            return None
        return sync(direction="both", config_path=config_path)
    except Exception as exc:                                # best-effort: 例外は外へ出さない
        _log_debug(f"auto-sync skipped: {exc}")
        return None
