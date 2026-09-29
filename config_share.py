"""Cloudflare R2 経由で repo 側 config を push/pull する（任意・best-effort）。

R2 が運ぶのは「同期ドライブに乗らない repo 側 config」だけ＝ dataset 登録簿
（`datasets.local.json`）＋ gitignore な repo 直下の portable files（models.toml / llm_backend/config.toml）と
（任意）.env。データセット側の analysis.py / _work / state / batch / myanalysis.toml は
同期ドライブで sync 済みなので bundle に含めない。

best-effort 原則: 自動経路はクレデンシャル未設定・boto3 未導入・ネット不通・バケットエラー・
remote 破損のいずれでも例外で呼び出し元を止めない（boto3 は short timeout＋再試行なし）。
自動経路は 2 種類:
  - try_sync（双方向）: GUI 起動時（tool.py）と CLI register-dataset の後。
  - try_push（送信のみ）: GUI での登録・登録削除の直後。gui/config_push.py の ConfigPusher が
    デーモンスレッドで実行する（GUI スレッドを塞がない。ローカルファイル・config.DATASETS には
    触れない）。PC ローカルの push ロックで直列化する。
明示 CLI（sync/push/pull）はエラーを表に出す。

登録簿のマージは union・非破壊で、remote が消えてもローカルは消えない。削除は
`config.unregister_dataset` が記録する明示的な tombstone（`deleted`: "ds/HOST" → 削除時刻）
だけが伝播させる（schema 2。tombstone を知らない旧コードは schema 2 を read-only 扱いにし、
削除済みエントリを push し返さない）。

boto3 はモジュール先頭では import せず _client() で遅延 import する。
"""
import json
import os
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

import config
from common.env import load_env
from common.filelock import exclusive_lock
from common.paths import atomic_write_text, repo_root

SCHEMA_VERSION = 2                 # 2: 登録削除の tombstone（deleted）を追加
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


def _newer(a: str | None, b: str | None) -> bool:
    """a が b より新しいと言えるときだけ True（どちらかが不明なら False）。"""
    pa, pb = _parse_iso(a), _parse_iso(b)
    return pa is not None and pb is not None and pa > pb


def _valid_tombs(tombs) -> dict:
    """時刻を解釈できる tombstone だけ残す（不正な削除記録でエントリを消さない）。"""
    if not isinstance(tombs, dict):
        return {}
    return {k: v for k, v in tombs.items()
            if isinstance(k, str) and isinstance(v, str) and _parse_iso(v) is not None}


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
            # client config の max_attempts は「再試行回数」（total = +1）。初回だけにするのは total_max_attempts=1。
            retries={"total_max_attempts": 1, "mode": "standard"},
        ),
    )


def _err_code(exc) -> str | None:
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        return resp.get("Error", {}).get("Code")
    return None


_NOT_FOUND = {"NoSuchKey", "404", "NotFound"}
_PRECONDITION = {"PreconditionFailed", "412"}


def _state_path() -> Path:
    return Path.home() / ".myanalysis" / "config_share_state.json"


def _local_state() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"entry_meta": {}, "etag": None, "deleted": {}}
    if not isinstance(data, dict):
        return {"entry_meta": {}, "etag": None, "deleted": {}}
    data.setdefault("entry_meta", {})
    data.setdefault("etag", None)
    data["deleted"] = _valid_tombs(data.get("deleted"))
    return data


def _save_local_state(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _update_local_state(config_path: Path | None, update) -> None:
    """登録簿ロック下で状態を読み直し、update(state) を適用して保存する（RMW）。

    note_deleted / note_registered は同じロック内で書くので、読み直しから保存までの間に
    割り込まれて失われることはない。ロックは非再入 — registry_transaction の中から呼ばないこと。
    """
    with config.registry_transaction(config_path=config_path):
        state = _local_state()
        update(state)
        _save_local_state(state)


def _stamp_after_known(state: dict, key: str) -> str:
    """key の新しいイベント（登録・削除）の時刻。

    ローカル状態に記録済みのその key の時刻（entry_meta と deleted の大きい方）より厳密に
    新しい値を返す（時計の同値・巻き戻りで、後の操作が先に push された操作に負けないように）。
    表現上限で繰り上げられないときは高水位そのもの（同値）を返し、例外は出さない。"""
    now = _now_iso()
    known = [dt for dt in (_parse_iso(state["entry_meta"].get(key)),
                           _parse_iso(state["deleted"].get(key))) if dt is not None]
    if not known:
        return now
    last = max(known)
    now_dt = _parse_iso(now)
    if now_dt is not None and now_dt > last:
        return now
    try:
        return (last + timedelta(microseconds=1)).isoformat()
    except OverflowError:                                   # 9999-12-31T23:59:59.999999 等
        _log_debug(f"cannot stamp after {last.isoformat()} for {key!r}; using it as-is")
        return last.isoformat()


def note_deleted(name: str, hosts) -> None:
    """登録解除した (name, HOST) を tombstone として記録する（次回 sync で remote/他 PC へ伝播）。

    `config.unregister_dataset` が登録簿ロック内から呼ぶ（sync はこのロック内で tombstone を
    読むので直列化される）。R2 未設定でも記録する — 後から設定したとき remote に残る古い
    登録で復活させないため。時刻は `_stamp_after_known` で採る（同じキーの既知の時刻より厳密に新しい）。
    """
    state = _local_state()
    for host in hosts:
        key = f"{name}/{host}"
        state["deleted"][key] = _stamp_after_known(state, key)   # entry_meta を消す前に採る
        state["entry_meta"].pop(key, None)
    _save_local_state(state)


def note_registered(name: str, host: str) -> None:
    """登録（上書き含む）した時刻を entry_meta に記録し、同キーのローカル tombstone を消す。

    `config.register_dataset` が登録簿ロック内から呼ぶ。この時刻が無いと、他 PC の削除後に
    （まだ sync していない PC で）登録し直したエントリが tombstone より古く見えて消される。
    時刻は `_stamp_after_known` で採る（同じキーの既知の時刻より厳密に新しい）。
    """
    state = _local_state()
    key = f"{name}/{host}"
    state["entry_meta"][key] = _stamp_after_known(state, key)    # tombstone を消す前に採る
    state["deleted"].pop(key, None)
    _save_local_state(state)


def make_bundle(*, config_path: Path | None = None, include_env: bool = False,
                warnings: list | None = None) -> dict:
    """ローカルの寄与を bundle dict にする（remote とは未マージ）。"""
    warnings = [] if warnings is None else warnings
    datasets = config.read_registry(config_path)

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
    for opt in ("entry_meta", "files", "file_meta", "deleted"):
        val = bundle.get(opt)
        if val is None:
            continue
        if not isinstance(val, dict):
            return False
        for k, v in val.items():
            if not isinstance(k, str) or not isinstance(v, str):
                return False
    try:
        json.dumps(bundle, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return False   # 孤立サロゲート（JSON の "\ud800" 等）。書込でも push でも encode できない
    return True


def _get_remote(client, bucket: str, key: str) -> tuple[dict | None, str | None, bool]:
    """戻り: (bundle, etag, unusable)。
      - NoSuchKey/404 -> (None, None, False)  # 空 remote（初回 push 可）
      - 取得成功 & schema_version <= SCHEMA_VERSION & _validate_bundle True
            -> (bundle, etag, False)
      - 取得成功だが schema_version > SCHEMA_VERSION か _validate_bundle False
            -> (None, etag, True)  # read-only
      - NoSuchBucket -> RuntimeError（バケット名の誤り。空 remote とは扱わない）
    """
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
    except Exception as exc:
        code = _err_code(exc)
        if code == "NoSuchBucket":
            raise RuntimeError(
                f"R2 bucket {bucket!r} not found (check R2_BUCKET in .env)") from exc
        if code in _NOT_FOUND:
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


def _expand_remote(remote: dict | None,
                   warnings: list) -> tuple[dict, dict, dict, dict, list, dict]:
    """remote（None=空 remote）を (datasets, entry_meta, files, file_meta, dropped, deleted)
    に展開。files/file_meta は _ALLOWED_FILES の whitelist 内のみ採用し、許可外キーは dropped に
    集め warning する。files の text は改行を LF にそろえて返す。"""
    remote = remote or {}
    datasets = remote.get("datasets") or {}
    entry_meta = remote.get("entry_meta") or {}
    deleted = _valid_tombs(remote.get("deleted"))
    raw_files = remote.get("files") or {}
    raw_file_meta = remote.get("file_meta") or {}
    files, file_meta, dropped = {}, {}, []
    for k, v in raw_files.items():
        if k in _ALLOWED_FILES:
            # CR を LF にそろえる（ローカルは universal newlines で読むので LF だけ）。そろえないと
            # マージで一致せず毎回書込予定に入り、atomic_write_text の読み戻し比較も一致しない。
            files[k] = v.replace("\r\n", "\n").replace("\r", "\n")
            if k in raw_file_meta:
                file_meta[k] = raw_file_meta[k]
        else:
            dropped.append(k)
            msg = f"remote files の許可外キーを無視: {k!r}"
            if msg not in warnings:
                warnings.append(msg)
    return datasets, entry_meta, files, file_meta, dropped, deleted


def _merge_datasets(local_reg, local_meta, remote_reg, remote_meta, self_host, now_iso,
                    local_tomb=None, remote_tomb=None):
    """戻り: (merged_reg, merged_meta, merged_tomb)。(dataset,HOST) の union。
    各キーの path と meta を決め、meta が None のキーは merged_meta に入れない（omit）。

    削除は tombstone（"ds/HOST" → 削除時刻）があるときだけ起こる — 片側に無いだけでは
    消さない（remote 消失でローカルを失わない）。meta は登録時刻（note_registered）:
      - local のみ live: remote tombstone が local tombstone より新しく（local に無い場合を
        含む）、local の meta がそれより新しくなければ削除。local tombstone と同じか古い
        remote tombstone は既知の削除なので、live なのは削除後の再追加として残す。
      - remote のみ live: local tombstone があり、remote の meta がそれより新しくなければ
        削除（自分の削除を押し出す）。新しければ削除後の再登録として採用。
    live で残ったキーの tombstone は落とし、どちらにも live でないキーの tombstone は新しい方を
    持ち越す。"""
    local_tomb = _valid_tombs(local_tomb)
    remote_tomb = _valid_tombs(remote_tomb)
    merged_reg: dict = {}
    merged_meta: dict = {}
    live: set = set()

    all_ds = set(local_reg) | set(remote_reg)
    for ds in all_ds:
        local_hosts = local_reg.get(ds, {})
        remote_hosts = remote_reg.get(ds, {})
        per_host: dict = {}
        dropped = False
        for host in set(local_hosts) | set(remote_hosts):
            key = f"{ds}/{host}"
            in_local = host in local_hosts
            in_remote = host in remote_hosts
            if in_local and not in_remote:
                tomb_at = remote_tomb.get(key)
                if (tomb_at is not None
                        and (key not in local_tomb or _newer(tomb_at, local_tomb[key]))
                        and not _newer(local_meta.get(key), tomb_at)):
                    dropped = True
                    continue
                per_host[host] = local_hosts[host]
                meta = local_meta.get(key) or now_iso
            elif in_remote and not in_local:
                if key in local_tomb and not _newer(remote_meta.get(key), local_tomb[key]):
                    dropped = True
                    continue
                per_host[host] = remote_hosts[host]
                meta = remote_meta.get(key)
            else:
                lp, rp = local_hosts[host], remote_hosts[host]
                if lp == rp:
                    per_host[host] = lp
                    meta = remote_meta.get(key) or local_meta.get(key)
                elif host == self_host:
                    per_host[host] = lp
                    # 登録時刻が remote より新しければそれを使う（sync 時刻で上書きすると、
                    # オフライン中の編集がその後の他 PC の削除より新しく見えてしまう）。
                    lm = local_meta.get(key)
                    keep_lm = (_parse_iso(lm) is not None
                               and not _newer(remote_meta.get(key), lm))
                    meta = lm if keep_lm else now_iso
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
            live.add(key)
            if meta is not None:
                merged_meta[key] = meta
        # 削除で host が空になった DS は消す（元から {} の DS は従来どおり残す）。
        if per_host or not dropped:
            merged_reg[ds] = per_host

    merged_tomb: dict = {}
    for key in (set(local_tomb) | set(remote_tomb)) - live:
        lt, rt = local_tomb.get(key), remote_tomb.get(key)
        merged_tomb[key] = lt if rt is None or _newer(lt, rt) else rt
    return merged_reg, merged_meta, merged_tomb


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

    # sidecar entry_meta（datasets の LWW 用）を取得するために make_bundle を使う。
    local = make_bundle(config_path=config_path, include_env=include_env, warnings=warnings)

    remote, etag, unusable = _get_remote(client, bucket, object_key)
    if unusable:
        warnings.append("remote bundle が未知スキーマ/不正のため read-only フォールバック")
        if apply:
            _update_local_state(config_path, lambda state: state.update(etag=etag))
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
    merged_tomb: dict = {}
    local_tomb: dict = {}
    local_meta: dict = {}
    new_entry_meta: dict | None = None

    for _attempt in range(PUT_RETRIES):
        (remote_datasets, remote_entry_meta, remote_files,
         remote_file_meta, dropped_file_keys, remote_tomb) = _expand_remote(remote, warnings)

        local_files, local_file_meta, unreadable_files = _collect_portable_files(
            include_env=include_env, warnings=warnings
        )

        merged_files, merged_file_meta, writes = _merge_files(
            local_files, local_file_meta, remote_files, remote_file_meta, now
        )

        wrote_local = False
        with config.registry_transaction(config_path=config_path) as (fresh_reg, writer):
            # tombstone と登録時刻は登録簿ロック内で fresh に読む（register/unregister_dataset
            # と直列化）。
            snap = _local_state()
            local_tomb, local_meta = snap["deleted"], snap["entry_meta"]
            merged_reg, merged_meta, merged_tomb = _merge_datasets(
                fresh_reg, local_meta, remote_datasets,
                remote_entry_meta, self_host, now, local_tomb, remote_tomb,
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
                target.parent.mkdir(parents=True, exist_ok=True)   # ロックファイルの置き場を先に作る
                with exclusive_lock(target.with_name(target.name + ".lock")):
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
                    atomic_write_text(target, text)
                    rdt = _parse_iso(rmeta)
                    if rdt is not None:
                        now_epoch = _parse_iso(now).timestamp()
                        os.utime(target, (now_epoch, rdt.timestamp()))

        if apply and direction in ("both", "pull") and wrote_local:
            new_entry_meta = merged_meta

        would_push = direction in ("both", "push") and (
            merged_reg != remote_datasets
            or merged_files != remote_files
            or merged_tomb != remote_tomb
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
            "deleted": merged_tomb,
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
        def _apply(state: dict) -> None:
            # マージに使った snapshot（local_meta / local_tomb）から変わったキーは、sync 中の
            # register/unregister_dataset が書いたものなので fresh 側を優先する。
            state["etag"] = etag
            if new_entry_meta is not None:
                em = dict(new_entry_meta)
                for k, v in state["entry_meta"].items():
                    if local_meta.get(k) != v:
                        em[k] = v
                state["entry_meta"] = em
            if direction in ("both", "pull"):
                # ローカル登録簿はマージ結果に揃っているので tombstone もマージ結果で置き換える。
                # push のみのときは削除がローカルに未適用なので local の tombstone を消費しない。
                tombs = dict(merged_tomb)
                for k, v in state["deleted"].items():
                    if local_tomb.get(k) != v:        # sync 中の削除（再削除を含む）
                        tombs[k] = v
                for k in set(local_tomb) - set(state["deleted"]):
                    tombs.pop(k, None)                # sync 中の再登録が消した tombstone
                state["deleted"] = tombs

        _update_local_state(config_path, _apply)

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


def autosync_enabled() -> bool:
    """自動同期（try_sync / try_push）を行う条件。ネットワークは叩かない。例外は False。

    GUI スレッドから呼んでよい（.env の小さな読取だけ）。"""
    try:
        load_env()
        if os.environ.get("R2_AUTOSYNC", "1") == "0":
            return False
        return is_configured()
    except Exception as exc:                                # best-effort
        _log_debug(f"auto-sync gate failed: {exc}")
        return False


def try_sync(*, config_path: Path | None = None) -> dict | None:
    """繋がるときだけ双方向収束。起動も登録も絶対に止めない。戻り値は無視してよい。"""
    try:
        if not autosync_enabled():
            return None
        return sync(direction="both", config_path=config_path)
    except Exception as exc:                                # best-effort: 例外は外へ出さない
        _log_debug(f"auto-sync skipped: {exc}")
        return None


def _push_lock_path() -> Path:
    """自動 push を PC ローカルで直列化するロック（状態ファイルと同じディレクトリ）。"""
    return _state_path().with_name("config_push.lock")


def try_push(*, config_path: Path | None = None) -> dict | None:
    """remote にだけ書く送信（never-raise）。GUI での登録・登録削除の直後にワーカースレッドから呼ぶ。

    datasets.local.json / config.DATASETS / portable files には触れない（書くのは remote と
    状態ファイルの etag だけ）。push の間は PC ローカルの push ロックを保持し、hot reload の
    世代をまたいでも同時に push するのは 1 本だけにする（待ちはワーカースレッドで起こる）。"""
    try:
        if not autosync_enabled():
            return None
        lock = _push_lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(lock):
            return sync(direction="push", config_path=config_path)
    except Exception as exc:                                # best-effort: 例外は外へ出さない
        _log_debug(f"auto-push skipped: {exc}")
        return None
