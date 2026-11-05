"""チャットの関連検索（埋め込み索引）— Qt 非依存（Issue #125。実験的・既定オフ）。

保存済みの ``<work_dir>/chat_sessions/*.json``（登録済みの全データセットのうち、この PC で届くもの）
を事前学習済みの埋め込みモデルで埋め込み、PC ローカルの numpy 索引を作って検索する。

配置（``llm_bridge.paths.chat_index_dir()`` の下。再計算できるので 1 コピーで書き、壊れていれば
空として扱って作り直す）::

    <slug>.lock                         更新・再構築・削除の排他（filelock）
    <slug>/meta.json                    {"version","model","dims","recipe","chunker","created"}
    <slug>/<dskey>/manifest.json        小さなポインタ＋ファイルの stat 表
    <slug>/<dskey>/rows-<h>.json        不変: {"sessions": {...}, "chunks": [...]}
    <slug>/<dskey>/vectors-<h>.npy      不変: float16 (n_chunks, dims)、行 i ↔ chunks[i]

slug は埋め込み器の model_id、dskey は sha1(データセット名)[:16]、<h> は rows と vectors の
バイト列の sha1[:16]（名前が中身を表すので、同名なら同じ中身）。読む側はロックを取らない:
manifest が指す組（だめなら previous の組、それもだめなら manifest を読み直して 1 回だけ再試行）を読む。

このモジュールは tr() を呼ばず、文を作らない（事実・数値・英語の code だけを返す。文は CLI と GUI）。
ソースに日本語の文字列リテラルを書かない（CJK の文字範囲はエスケープで書く）。依存は
モジュールを取り込んで属性で呼ぶ（テストの差し替えが効くように）。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
import re
import shutil
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from common import embed_model, filelock
from common import paths as cpaths
from llm_bridge import chat_search, chat_store
from llm_bridge import paths as lb_paths

NotReady = embed_model.NotReady
Cancelled = embed_model.Cancelled

PREF_ENABLED = "chat_recall_enabled"
PREF_MODEL = "chat_recall_model"
PREF_IN_AI_SEARCH = "chat_recall_in_ai_search"

VERSION = 1
CHUNKER_VERSION = 1
MAX_K = 200
SNIPPET_CHARS = 300
_CANDIDATES = 100
_RRF_K = 60
_EMBED_GROUP = 64          # まとめて埋め込む新しい本文の数の目安（予算切れのときの公開の単位）

_monotonic = time.monotonic   # テストが差し替える時計


class IndexLocked(RuntimeError):
    """待つ側のロック（更新・再構築・削除）が上限まで待っても取れなかった。"""


# ---------------------------------------------------------------- 設定・準備状態・埋め込み器


@dataclass(frozen=True)
class RecallPrefs:
    enabled: bool
    model_id: str
    in_ai_search: bool


def load_prefs() -> RecallPrefs:
    """ui_prefs の 3 キー。型が違う・catalog に無い値は既定に戻す。never raise。"""
    enabled = lb_paths.read_ui_pref(PREF_ENABLED, False)
    model_id = lb_paths.read_ui_pref(PREF_MODEL, embed_model.DEFAULT_MODEL)
    in_ai = lb_paths.read_ui_pref(PREF_IN_AI_SEARCH, True)
    if not isinstance(enabled, bool):
        enabled = False
    if not isinstance(model_id, str) or model_id not in embed_model.CATALOG:
        model_id = embed_model.DEFAULT_MODEL
    if not isinstance(in_ai, bool):
        in_ai = True
    return RecallPrefs(enabled, model_id, in_ai)


def readiness() -> embed_model.Readiness:
    """選択中のモデルについて、依存とモデルの状態（ネットワーク・推論なし）。"""
    return embed_model.readiness(load_prefs().model_id)


def is_ready() -> bool:
    """有効 かつ 依存が import できる かつ 選択中のモデルが導入済み。"""
    if not load_prefs().enabled:
        return False
    r = readiness()
    return not r.deps_missing and r.model_state == "installed"


_EMBEDDER = None
_EMBEDDER_LOCK = threading.Lock()


def make_embedder():
    """プロセス内で 1 つの埋め込み器をキャッシュして返す。準備できていなければ NotReady。"""
    global _EMBEDDER
    prefs = load_prefs()
    if not prefs.enabled:
        raise NotReady("disabled")
    with _EMBEDDER_LOCK:
        cur = _EMBEDDER
        if cur is not None and getattr(cur, "model_id", None) == prefs.model_id:
            return cur
        if cur is not None:
            _EMBEDDER = None
            _close(cur)
        emb = embed_model.OnnxEmbedder(embed_model.CATALOG[prefs.model_id],
                                       embed_model.install_root())
        _EMBEDDER = emb
        return emb


def release_embedder() -> None:
    """キャッシュした埋め込み器を閉じて捨てる（モデルの削除・更新の前）。"""
    global _EMBEDDER
    with _EMBEDDER_LOCK:
        cur, _EMBEDDER = _EMBEDDER, None
    if cur is not None:
        _close(cur)


def _close(emb) -> None:
    close = getattr(emb, "close", None)
    if callable(close):
        with contextlib.suppress(Exception):
            close()


# ---------------------------------------------------------------- チャンク分割と語彙


@dataclass(frozen=True)
class ChunkLimits:
    target: int      # 詰めるときの上限（文字）
    hard: int        # これを超える段落は硬分割する
    overlap: int     # 硬分割の重なり


def chunk_limits(model) -> ChunkLimits:
    """モデルの最大トークン数に合わせた幅。catalog に無いモデル（None）は既定。"""
    if model is not None and model.max_tokens <= 512:
        return ChunkLimits(450, 600, 80)
    return ChunkLimits(600, 800, 80)


_PARA_SPLIT = re.compile(r"\n\s*\n")


def _hard_split(p: str, limits: ChunkLimits) -> list[str]:
    out: list[str] = []
    n = len(p)
    start = 0
    while True:
        end = start + limits.target
        if end >= n:                                   # 最後の窓は空白へ戻さない
            piece = p[start:].strip()
            if piece:
                out.append(piece)
            return out
        lo = start + limits.target // 2
        cut = end
        for i in range(end - 1, lo - 1, -1):           # 切れ目は直近の空白へ戻す
            if p[i].isspace():
                cut = i
                break
        piece = p[start:cut].strip()
        if piece:
            out.append(piece)
        nxt = max(cut - limits.overlap, start + 1)     # cut >= start + target//2 > start + overlap
        if nxt > 0 and not p[nxt - 1].isspace():       # 重なりの始まりも語の途中にしない
            for j in range(nxt, cut):
                if p[j].isspace():
                    nxt = j + 1
                    break
        start = nxt


def chunk_text(text: str, limits: ChunkLimits) -> list[str]:
    """空行で段落に分け、target 以下に貪欲に詰める。hard を超える段落は重なりつきで硬分割する。決定的。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paras = [p.strip() for p in _PARA_SPLIT.split(text)]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if not p:
            continue
        if len(p) > limits.hard:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(_hard_split(p, limits))
        elif len(p) > limits.target:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(p)
        elif buf and len(buf) + 2 + len(p) <= limits.target:
            buf = buf + "\n\n" + p
        else:
            if buf:
                chunks.append(buf)
            buf = p
    if buf:
        chunks.append(buf)
    return chunks


# 平仮名・片仮名・CJK 統合漢字（拡張 A を含む）・互換漢字（ソースに CJK の文字を書かないためエスケープ）
_CJK = re.compile("[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_IDENT = re.compile(r"[_\-0-9]")


def normalize(text: str) -> str:
    """NFKC → casefold。"""
    return unicodedata.normalize("NFKC", text).casefold()


def lexical_terms(query: str) -> list[tuple[str, tuple[str, ...]]]:
    """問い合わせの語を語彙シグナルの項にする。

    ``_``・``-``・数字を含む語 → ("sub", (語,))（部分一致）、CJK を含む語 → ("grams", 2-gram…)、
    それ以外 → ("sub", (語,))。"""
    terms: list[tuple[str, tuple[str, ...]]] = []
    for w in chat_search.split_terms(normalize(query)):
        if _IDENT.search(w):
            terms.append(("sub", (w,)))
        elif _CJK.search(w):
            grams = (w,) if len(w) == 1 else tuple(dict.fromkeys(w[i:i + 2]
                                                                 for i in range(len(w) - 1)))
            terms.append(("grams", grams))
        else:
            terms.append(("sub", (w,)))
    return terms


def lexical_score(norm_text: str, terms) -> float:
    """項ごとの充足率の平均（sub は 0/1、grams は含まれる 2-gram の割合）。"""
    if not terms:
        return 0.0
    total = 0.0
    for kind, items in terms:
        if kind == "sub":
            total += 1.0 if items[0] in norm_text else 0.0
        else:
            total += sum(1 for g in items if g in norm_text) / len(items)
    return total / len(terms)


# ---------------------------------------------------------------- 報告の型


@dataclass(frozen=True)
class IndexWarning:
    code: str                  # locked / budget / cancelled / unreachable / work_dir / scan /
    dataset: str | None = None  # write / unreadable / unknown / shard-invalid
    detail: str = ""

    def to_dict(self) -> dict:
        return {"code": self.code, "dataset": self.dataset, "detail": self.detail}


_WARN_REASONS = ("unreachable", "work_dir", "scan", "write", "unknown")


@dataclass
class DatasetReport:
    name: str
    state: str                 # unchanged / updated / removed / skipped
    reason: str | None = None  # skipped・partial の理由（no-host / unreachable / … / budget）
    sessions: int = 0
    chunks: int = 0
    embedded: int = 0
    reused: int = 0
    unreadable: int = 0
    partial: bool = False
    detail: str = ""


@dataclass
class UpdateReport:
    ran: bool
    cancelled: bool
    datasets: list[DatasetReport] = field(default_factory=list)
    removed_shards: int = 0
    removed_slugs: int = 0
    elapsed_s: float = 0.0

    def warnings(self) -> list[IndexWarning]:
        out: list[IndexWarning] = []
        if not self.ran:
            return [IndexWarning("locked")]
        budget: list[str] = []
        for d in self.datasets:
            if d.reason in _WARN_REASONS:
                out.append(IndexWarning(d.reason, d.name, d.detail))
            if d.unreadable:
                out.append(IndexWarning("unreadable", d.name, str(d.unreadable)))
            if d.reason == "budget":
                budget.append(d.name)
        if budget:
            out.append(IndexWarning("budget", None, ", ".join(budget)))
        if self.cancelled:
            out.append(IndexWarning("cancelled"))
        return out

    def to_dict(self) -> dict:
        return {"ran": self.ran, "cancelled": self.cancelled,
                "elapsed_s": round(self.elapsed_s, 3),
                "removed_shards": self.removed_shards, "removed_slugs": self.removed_slugs,
                "datasets": [asdict(d) for d in self.datasets],
                "warnings": [w.to_dict() for w in self.warnings()]}


@dataclass(frozen=True)
class RecallHit:
    sid: str
    sid8: str
    idx: int
    dataset: str
    title: str
    archived: bool
    role: str
    score: float
    cosine: float
    lexical: float
    snippet: str
    updated: float

    def to_dict(self) -> dict:
        return {"sid": self.sid8, "idx": self.idx, "dataset": self.dataset, "title": self.title,
                "archived": self.archived, "role": self.role, "score": round(self.score, 4),
                "cosine": round(self.cosine, 4), "lexical": round(self.lexical, 3),
                "snippet": self.snippet, "updated": chat_search.format_ts(self.updated)}


@dataclass
class RecallResult:
    hits: list[RecallHit]
    updated_index: bool
    stale: dict[str, int]
    warnings: list[IndexWarning]


@dataclass
class ShardStatus:
    name: str
    sessions: int = 0
    chunks: int = 0
    bytes: int = 0
    updated: float | None = None
    stale: int = 0


@dataclass
class IndexStatus:
    enabled: bool
    ready: bool
    model: str
    deps_missing: list[str]
    model_state: str
    slug_dir: str
    dims: int | None
    datasets: list[ShardStatus]
    total_chunks: int
    total_bytes: int
    last_update: float | None
    stale: dict[str, int]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["last_update"] = (chat_search.format_ts(self.last_update)
                            if self.last_update is not None else None)
        for s in d["datasets"]:
            s["updated"] = chat_search.format_ts(s["updated"]) if s["updated"] is not None else None
        return d


# ---------------------------------------------------------------- 配置と検証

_SLUG = re.compile(r"[a-z0-9][a-z0-9._-]*")
_ROWS_NAME = re.compile(r"rows-[0-9a-f]{16}\.json")
_VECS_NAME = re.compile(r"vectors-[0-9a-f]{16}\.npy")
_DSKEY = re.compile(r"[0-9a-f]{16}")
_SID_PREFIX = re.compile(r"[0-9a-f]{8,32}")
_SID_SAFE = re.compile(r"[0-9a-f][0-9A-Za-z_-]*")


@dataclass(frozen=True)
class _Spec:
    """manifest を照合する値。check=False は読むだけの呼び出し（catalog 外のモデル）で、dims と
    recipe を照合しない。"""
    slug: str
    dims: int | None
    recipe: str | None
    check: bool = True


def _spec_for(model_id: str) -> _Spec:
    m = embed_model.CATALOG.get(model_id)
    if m is None:
        return _Spec(model_id, None, None, check=False)
    return _Spec(model_id, m.dims, m.recipe)


def dskey(dataset: str) -> str:
    return hashlib.sha1(dataset.encode("utf-8")).hexdigest()[:16]


def _slug_dir(slug: str) -> Path:
    return lb_paths.chat_index_dir() / slug


def _shard_dir(slug: str, dataset: str) -> Path:
    return _slug_dir(slug) / dskey(dataset)


def _lock_file(slug: str) -> Path:
    return lb_paths.chat_index_dir() / f"{slug}.lock"


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _pair_ok(rows, vectors) -> bool:
    return (isinstance(rows, str) and isinstance(vectors, str)
            and _ROWS_NAME.fullmatch(rows) is not None and _VECS_NAME.fullmatch(vectors) is not None)


def _stat_ok(s) -> bool:
    return s is None or (isinstance(s, dict) and _is_int(s.get("size")) and s["size"] >= 0
                         and _is_int(s.get("mtime_ns")))


def _file_entry_ok(v) -> bool:
    if not isinstance(v, dict):
        return False
    js, bk = v.get("json"), v.get("bak")
    if not (_stat_ok(js) and _stat_ok(bk)) or (js is None and bk is None):
        return False
    fp = v.get("fingerprint")
    if fp is not None and not isinstance(fp, str):
        return False
    return isinstance(v.get("indexed"), bool) and isinstance(v.get("unreadable"), bool)


def _manifest_ok(m, dataset: str, spec: _Spec) -> bool:
    if not isinstance(m, dict) or m.get("version") != VERSION:
        return False
    if m.get("model") != spec.slug or m.get("dataset") != dataset:
        return False
    if m.get("chunker") != CHUNKER_VERSION or not isinstance(m.get("work_dir"), str):
        return False
    if not _is_int(m.get("dims")) or not _is_int(m.get("n_chunks")) or not _is_num(m.get("updated")):
        return False
    if spec.check and (m.get("dims") != spec.dims or m.get("recipe") != spec.recipe):
        return False
    if not _pair_ok(m.get("rows"), m.get("vectors")):
        return False
    prev = m.get("previous")
    if prev is not None and not (isinstance(prev, dict)
                                 and _pair_ok(prev.get("rows"), prev.get("vectors"))):
        return False
    files = m.get("files")
    if not isinstance(files, dict):
        return False
    return all(isinstance(k, str) and _file_entry_ok(v) for k, v in files.items())


def _read_manifest(shard_dir: Path, dataset: str, spec: _Spec) -> dict | None:
    try:
        m = json.loads((shard_dir / "manifest.json").read_bytes().decode("utf-8"))
    except Exception:  # noqa: BLE001 — 読めない・壊れている manifest は無いものとして扱う
        return None
    return m if _manifest_ok(m, dataset, spec) else None


@dataclass
class _Data:
    sessions: dict
    chunks: list
    vectors: np.ndarray            # float32 (n, dims)
    norm: list[str] | None = None  # 正規化した本文（検索で初めて作る）

    def norm_texts(self) -> list[str]:
        if self.norm is None:
            self.norm = [normalize(c["text"]) for c in self.chunks]
        return self.norm


def _session_meta_ok(v) -> bool:
    return (isinstance(v, dict) and isinstance(v.get("fingerprint"), str)
            and _is_num(v.get("updated")) and isinstance(v.get("title"), str)
            and isinstance(v.get("archived"), bool))


def _chunk_ok(c, sessions: dict) -> bool:
    if not isinstance(c, dict):
        return False
    sid = c.get("sid")
    if not chat_search.agent_safe_id(sid) or sid not in sessions:
        return False
    if not _is_int(c.get("idx")) or c["idx"] < 0 or not _is_int(c.get("pos")) or c["pos"] < 0:
        return False
    text = c.get("text")
    return c.get("role") in ("user", "assistant") and isinstance(text, str) and bool(text)


def _parse_pair(rows_path: Path, vecs_path: Path, dims: int) -> _Data | None:
    """rows と vectors を読んで検証する。どこかが外れたら None。never raise。"""
    try:
        raw = json.loads(rows_path.read_bytes().decode("utf-8"))
        if not isinstance(raw, dict):
            return None
        sessions, chunks = raw.get("sessions"), raw.get("chunks")
        if not isinstance(sessions, dict) or not isinstance(chunks, list):
            return None
        if not all(isinstance(k, str) and _session_meta_ok(v) for k, v in sessions.items()):
            return None
        if not all(_chunk_ok(c, sessions) for c in chunks):
            return None
        arr = np.load(io.BytesIO(vecs_path.read_bytes()), allow_pickle=False)   # 0 バイトは EOFError
        if not isinstance(arr, np.ndarray) or arr.ndim != 2 \
                or arr.shape != (len(chunks), dims) or not np.issubdtype(arr.dtype, np.floating):
            return None
        vec = arr.astype(np.float32)
        if not np.isfinite(vec).all():
            return None
        return _Data(sessions, chunks, vec)
    except Exception:  # noqa: BLE001 — 0 バイトの npy は EOFError など、どれも「壊れている」
        return None


_SHARD_CACHE: dict[str, tuple[tuple, _Data]] = {}
_CACHE_LOCK = threading.Lock()


def _load_pair(shard_dir: Path, rows: str, vectors: str, dims: int) -> _Data | None:
    """組を読む（stat が同じならキャッシュを使う。その場で壊されたファイルは stat が変わる）。"""
    rp, vp = shard_dir / rows, shard_dir / vectors
    try:
        rs, vs = rp.stat(), vp.stat()
    except OSError:
        return None
    key = (rows, vectors, rs.st_size, rs.st_mtime_ns, vs.st_size, vs.st_mtime_ns, dims)
    ck = str(shard_dir)
    with _CACHE_LOCK:
        hit = _SHARD_CACHE.get(ck)
    if hit is not None and hit[0] == key:
        return hit[1]
    data = _parse_pair(rp, vp, dims)
    if data is not None:
        with _CACHE_LOCK:
            _SHARD_CACHE[ck] = (key, data)
    return data


def _forget_cache(prefix: Path) -> None:
    p = str(prefix)
    with _CACHE_LOCK:
        for k in [k for k in _SHARD_CACHE if k == p or k.startswith(p + os.sep)]:
            del _SHARD_CACHE[k]


def _rmtree(p: Path) -> None:
    shutil.rmtree(p, ignore_errors=True)
    _forget_cache(p)


# ---------------------------------------------------------------- データセットの解決と走査


def _targets(datasets, reload: bool) -> tuple[list[str], list[str]]:
    import config
    if reload:
        config.reload_datasets()                     # RegistryError は送出
    registered = set(config.DATASETS)
    if datasets is None:
        return sorted(registered), []
    known: list[str] = []
    unknown: list[str] = []
    for n in dict.fromkeys(datasets):
        (known if n in registered else unknown).append(n)
    return known, unknown


@dataclass
class _Where:
    work_dir: Path | None
    reason: str | None = None       # no-host / unreachable / work_dir
    detail: str = ""


def _locate(name: str) -> _Where:
    """データセットの work_dir（読むだけ。作らない・resolve しない）。"""
    import config
    import dataset_config
    try:
        ds_dir = config.get_dataset_dir(name)
    except (KeyError, RuntimeError) as e:
        return _Where(None, "no-host", str(e))
    try:
        ok = ds_dir.is_dir()
    except OSError:
        ok = False
    if not ok:
        return _Where(None, "unreachable", str(ds_dir))
    try:
        wd = dataset_config.get_work_dir(name, create=False)
    except (KeyError, RuntimeError, ValueError, OSError) as e:
        return _Where(None, "work_dir", f"{type(e).__name__}: {e}")
    return _Where(wd)


def _scan(cs_dir: Path) -> dict[str, dict]:
    """``<stem>.json`` と ``<stem>.json.bak`` の stat（DirEntry から。ファイルは読まない）。"""
    out: dict[str, dict] = {}
    try:
        it = os.scandir(cs_dir)
    except FileNotFoundError:
        return out
    with it:
        for e in it:
            name = e.name
            if name.endswith(".json.bak"):
                stem, kind = name[:-len(".json.bak")], "bak"
            elif name.endswith(".json"):
                stem, kind = name[:-len(".json")], "json"
            else:
                continue
            if not stem:
                continue
            try:
                if not e.is_file():
                    continue
                st = e.stat()
            except OSError:
                continue
            ent = out.setdefault(stem, {"json": None, "bak": None})
            ent[kind] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    return out


def _stale_count(scan: dict, manifest: dict | None, work_dir: Path) -> int:
    if manifest is None or manifest.get("work_dir") != str(work_dir):
        return len(scan)
    files = manifest["files"]
    n = sum(1 for s in files if s not in scan)
    for stem, st in scan.items():
        old = files.get(stem)
        if old is None or old["json"] != st["json"] or old["bak"] != st["bak"]:
            n += 1
    return n


# ---------------------------------------------------------------- 索引


def _rank_desc(x: np.ndarray) -> np.ndarray:
    """同点を同じ順位にする順位（1 + 自分より大きい値の数）。"""
    s = np.sort(x)
    return 1 + (len(x) - np.searchsorted(s, x, side="right"))


def _session_meta(sess, fingerprint: str) -> dict:
    title = sess.title if isinstance(sess.title, str) else ""
    return {"fingerprint": fingerprint, "updated": chat_search._updated_value(sess),
            "title": title, "archived": bool(getattr(sess, "archived", False))}


class ChatIndex:
    """1 つの埋め込み器（= 1 つの slug）に対する索引の操作。"""

    def __init__(self, embedder):
        slug = getattr(embedder, "model_id", None)
        if not isinstance(slug, str) or _SLUG.fullmatch(slug) is None:
            raise ValueError(f"invalid model id for the chat index: {slug!r}")
        self.embedder = embedder
        self.slug = slug
        self.dims = int(embedder.dims)
        self.recipe = getattr(embedder, "recipe", None)
        self.model = embed_model.CATALOG.get(slug)
        self.limits = chunk_limits(self.model)
        self.spec = _Spec(slug, self.dims, self.recipe)

    @classmethod
    def open(cls, embedder=None) -> ChatIndex:
        return cls(embedder if embedder is not None else make_embedder())

    # ---- ロック

    @contextlib.contextmanager
    def _acquire(self, blocking: bool):
        root = lb_paths.chat_index_dir()
        root.mkdir(parents=True, exist_ok=True)          # ロックの前に親を作る
        lock = _lock_file(self.slug)
        if not blocking:
            with filelock.try_exclusive_lock(lock) as got:
                yield got
            return
        with _blocking_lock(lock):
            yield True

    # ---- 更新

    def update(self, datasets=None, *, progress: Callable | None = None,
               cancel: Callable[[], bool] | None = None, blocking: bool = True,
               budget_s: float | None = None, reload: bool = True) -> UpdateReport:
        """保存済みのチャットとの差分だけ埋め込んで公開する。"""
        return self._run(datasets, progress=progress, cancel=cancel, blocking=blocking,
                         budget_s=budget_s, reload=reload, wipe=False)

    def rebuild(self, datasets=None, *, progress: Callable | None = None,
                cancel: Callable[[], bool] | None = None, blocking: bool = True,
                reload: bool = True) -> UpdateReport:
        """対象の shard を消してから作り直す（ロックは 1 回だけ取る）。"""
        return self._run(datasets, progress=progress, cancel=cancel, blocking=blocking,
                         budget_s=None, reload=reload, wipe=True)

    def _run(self, datasets, *, progress, cancel, blocking, budget_s, reload, wipe) -> UpdateReport:
        if budget_s is not None and not (_is_num(budget_s) and budget_s > 0):
            raise ValueError(f"budget_s must be a positive finite number: {budget_s!r}")
        t0 = time.perf_counter()
        targets, unknown = _targets(datasets, reload)
        with self._acquire(blocking) as got:
            if not got:
                return UpdateReport(False, False,
                                    [DatasetReport(n, "skipped", "locked") for n in targets],
                                    elapsed_s=time.perf_counter() - t0)
            deadline = _monotonic() + budget_s if budget_s is not None else None
            if wipe:
                if datasets is None:
                    _rmtree(_slug_dir(self.slug))
                else:
                    for n in targets:
                        _rmtree(_shard_dir(self.slug, n))
            report = self._update_locked(targets, unknown, progress=progress, cancel=cancel,
                                         deadline=deadline)
        report.elapsed_s = time.perf_counter() - t0
        return report

    def _ensure_meta(self) -> None:
        d = _slug_dir(self.slug)
        want = {"version": VERSION, "model": self.slug, "dims": self.dims, "recipe": self.recipe,
                "chunker": CHUNKER_VERSION}
        try:
            m = json.loads((d / "meta.json").read_bytes().decode("utf-8"))
        except Exception:  # noqa: BLE001 — 読めなければ作り直す
            m = None
        if isinstance(m, dict) and all(m.get(k) == v for k, v in want.items()) \
                and _is_int(m.get("dims")):
            return
        if d.exists():
            _rmtree(d)                     # 消し残りは manifest の照合で無効になる
        d.mkdir(parents=True, exist_ok=True)
        cpaths.atomic_write_text(d / "meta.json",
                                 json.dumps({**want, "created": time.time()}, indent=2))

    def _update_locked(self, targets, unknown, *, progress, cancel, deadline) -> UpdateReport:
        import config
        report = UpdateReport(True, False)
        self._ensure_meta()
        for n in unknown:
            report.datasets.append(DatasetReport(n, "skipped", "unknown"))
        stop: str | None = None
        for name in targets:
            if stop is None and deadline is not None and _monotonic() >= deadline:
                stop = "budget"
            if stop is not None:
                report.datasets.append(DatasetReport(name, "skipped", stop))
                continue
            dr = self._update_dataset(name, progress=progress, cancel=cancel, deadline=deadline)
            report.datasets.append(dr)
            if dr.reason == "cancelled":
                stop = "cancelled"
                report.cancelled = True
            elif dr.reason == "budget":
                stop = "budget"
        if report.cancelled:
            return report                  # 利用者の cancel では何も書かない（掃除もしない）
        # 登録から消えたデータセットの shard と、今の slug 以外の slug（再計算できるゴミ）
        keep = {dskey(n) for n in config.DATASETS}
        sd = _slug_dir(self.slug)
        with contextlib.suppress(OSError):
            for p in list(sd.iterdir()):
                if p.is_dir() and _DSKEY.fullmatch(p.name) and p.name not in keep:
                    _rmtree(p)
                    report.removed_shards += 1
        root = lb_paths.chat_index_dir()
        with contextlib.suppress(OSError):
            for p in list(root.iterdir()):
                if not p.is_dir() or p.name == self.slug or _SLUG.fullmatch(p.name) is None:
                    continue
                with filelock.try_exclusive_lock(root / f"{p.name}.lock") as got:
                    if got:
                        _rmtree(p)
                        report.removed_slugs += 1
        return report

    def _update_dataset(self, name: str, *, progress, cancel, deadline) -> DatasetReport:
        def user_cancelled() -> bool:
            return bool(cancel is not None and cancel())

        where = _locate(name)
        shard = _shard_dir(self.slug, name)
        if where.work_dir is None:
            return DatasetReport(name, "skipped", where.reason, detail=where.detail)
        work_dir = where.work_dir
        try:
            wd_ok = work_dir.is_dir()
        except OSError:
            wd_ok = False
        if not wd_ok:
            if shard.exists():             # 外れているだけかもしれないので shard は保持する
                return DatasetReport(name, "skipped", "unreachable", detail=str(work_dir))
            return DatasetReport(name, "unchanged")
        cs_dir = work_dir / "chat_sessions"
        try:
            scan = _scan(cs_dir)
        except OSError as e:
            return DatasetReport(name, "skipped", "scan", detail=f"{type(e).__name__}: {e}")

        # ---- 旧い shard
        old_m = _read_manifest(shard, name, self.spec)
        cur = prev = None
        if old_m is not None:
            cur = _load_pair(shard, old_m["rows"], old_m["vectors"], self.dims)
            if cur is None and old_m["previous"] is not None:
                prev = _load_pair(shard, old_m["previous"]["rows"], old_m["previous"]["vectors"],
                                  self.dims)
        trusted = old_m is not None and cur is not None and old_m["work_dir"] == str(work_dir)
        old_files: dict = old_m["files"] if trusted else {}
        old_sessions: dict = cur.sessions if cur is not None else {}

        if not scan:
            if shard.exists():
                _rmtree(shard)
                return DatasetReport(name, "removed")
            return DatasetReport(name, "unchanged")

        changed = [s for s, st in scan.items()
                   if (old := old_files.get(s)) is None or old["json"] != st["json"]
                   or old["bak"] != st["bak"] or old["unreadable"]]
        deleted = [s for s in old_files if s not in scan]
        if trusted and not changed and not deleted:
            return DatasetReport(name, "unchanged",
                                 sessions=sum(1 for v in old_files.values() if v["indexed"]),
                                 chunks=len(cur.chunks))

        # 旧い行（sid → 行番号）と、本文 → ベクトルの再利用の材料
        old_rows: dict[str, list[int]] = {}
        if cur is not None:
            for i, c in enumerate(cur.chunks):
                old_rows.setdefault(c["sid"], []).append(i)
        pool: dict[str, np.ndarray] = {}
        for src in (cur, prev):
            if src is not None:
                for i, c in enumerate(src.chunks):
                    pool.setdefault(c["text"], src.vectors[i])

        files: dict[str, dict] = {}
        metas: dict[str, dict] = {}
        rows: dict[str, list[tuple[dict, np.ndarray | None]]] = {}
        unreadable = 0

        def carry(sid: str) -> None:
            if cur is not None and sid in cur.sessions:
                metas[sid] = cur.sessions[sid]
                rows[sid] = [(cur.chunks[i], cur.vectors[i]) for i in old_rows.get(sid, [])]

        changed_set = set(changed)
        for stem, st in scan.items():
            if stem not in changed_set:
                ent = old_files[stem]
                files[stem] = ent
                if ent["indexed"]:
                    carry(stem)
        work: list[tuple[float, str, object]] = []          # (updated, stem, sess)
        for stem in changed:
            st = scan[stem]
            sess = chat_store.read_session_file(cs_dir / f"{stem}.json")
            fp = None
            if sess is not None:
                try:
                    fp = chat_store.content_fingerprint(sess)
                except Exception:  # noqa: BLE001 — 指紋が取れない中身は読めないものと同じ扱い
                    sess = None
            if sess is None:                                 # 両方読めない → 旧い行を保持し次回も読む
                unreadable += 1
                had = stem in old_sessions
                files[stem] = {**st, "fingerprint": old_sessions[stem]["fingerprint"] if had else None,
                               "indexed": had, "unreadable": True}
                if had:
                    carry(stem)
                continue
            if getattr(sess, "kind", "chat") == "search" or not chat_search.agent_safe_id(sess.id) \
                    or sess.id != stem:
                files[stem] = {**st, "fingerprint": None, "indexed": False, "unreadable": False}
                continue
            files[stem] = {**st, "fingerprint": fp, "indexed": True, "unreadable": False}
            if stem in old_sessions and old_sessions[stem]["fingerprint"] == fp:
                carry(stem)
                continue
            work.append((chat_search._updated_value(sess), stem, sess))

        # ---- 埋め込み（新しいチャットから。終わったセッションまでを公開できるように）
        work.sort(key=lambda w: -w[0])
        pending: dict[str, tuple[dict, list[dict]]] = {}
        for _u, stem, sess in work:
            new_rows = []
            for i, m in enumerate(sess.messages):
                text = chat_search.searchable_text(m)
                if text is None:
                    continue
                for pos, piece in enumerate(chunk_text(text, self.limits)):
                    new_rows.append({"sid": stem, "idx": i, "role": m.role, "pos": pos,
                                     "text": piece})
            pending[stem] = (_session_meta(sess, files[stem]["fingerprint"]), new_rows)
        total_new = len({r["text"] for _m, rs in pending.values() for r in rs
                         if r["text"] not in pool})
        done_texts = 0
        embedded = 0
        fresh: set[str] = set()
        partial = False
        order = [stem for _u, stem, _s in work]
        k = 0

        def budget_over() -> bool:
            return deadline is not None and _monotonic() >= deadline

        # 予算はグループの間でだけ見る（埋め込みの途中では止めない）。途中で止めると、予算より
        # 大きいセッションがいつまでも公開されない。1 回の更新で少なくとも 1 グループは公開し、
        # 予算を越えるのは最大 1 グループ分。利用者の cancel はバッチの間で止める。
        while k < len(order):
            if k > 0 and budget_over():
                partial = True
                break
            group: list[str] = []
            texts: list[str] = []
            seen: set[str] = set()
            while k < len(order) and len(texts) < _EMBED_GROUP:
                stem = order[k]
                k += 1
                group.append(stem)
                for r in pending[stem][1]:
                    t = r["text"]
                    if t not in pool and t not in seen:
                        seen.add(t)
                        texts.append(t)
            if texts:
                base = done_texts

                def _prog(d, n, base=base):
                    if progress is not None:
                        progress(name, base + d, total_new)

                try:
                    vecs = self.embedder.embed_documents(texts, progress=_prog,
                                                         cancel=user_cancelled)
                except Cancelled:
                    return DatasetReport(name, "skipped", "cancelled")
                vecs = np.asarray(vecs, dtype=np.float32)
                if vecs.shape != (len(texts), self.dims):
                    raise RuntimeError(f"embedder returned shape {vecs.shape}")
                for t, v in zip(texts, vecs):
                    pool[t] = v
                embedded += len(texts)
                fresh.update(texts)
                done_texts += len(texts)
            for stem in group:
                meta, new_rows = pending[stem]
                metas[stem] = meta
                rows[stem] = [(r, pool[r["text"]]) for r in new_rows]
        if partial:                                          # 済んでいない変更は旧い記録のまま
            for stem in order[k:]:
                if stem in old_files:
                    files[stem] = old_files[stem]
                    metas.pop(stem, None)
                    rows.pop(stem, None)
                    if old_files[stem]["indexed"]:
                        carry(stem)
                else:
                    files.pop(stem, None)
                    metas.pop(stem, None)
                    rows.pop(stem, None)
        if user_cancelled():                                 # 最後のバッチの最中の cancel
            return DatasetReport(name, "skipped", "cancelled")

        # ---- 組み立て
        flat = sorted(((r, v) for sid in rows for r, v in rows[sid]),
                      key=lambda rv: (rv[0]["sid"], rv[0]["idx"], rv[0]["pos"]))
        chunks = [{"sid": r["sid"], "idx": r["idx"], "role": r["role"], "pos": r["pos"],
                   "text": r["text"]} for r, _v in flat]
        sessions = {sid: metas[sid] for sid in sorted(metas)}
        vec = (np.stack([v for _r, v in flat]).astype(np.float16) if flat
               else np.zeros((0, self.dims), dtype=np.float16))
        rows_bytes = json.dumps({"sessions": sessions, "chunks": chunks}, ensure_ascii=False,
                                sort_keys=True, separators=(",", ":")).encode("utf-8")
        buf = io.BytesIO()
        np.save(buf, vec, allow_pickle=False)
        vec_bytes = buf.getvalue()
        h = hashlib.sha1(rows_bytes + vec_bytes).hexdigest()[:16]
        rows_name, vecs_name = f"rows-{h}.json", f"vectors-{h}.npy"
        n_sessions = sum(1 for v in files.values() if v["indexed"])
        dr = DatasetReport(name, "updated", "budget" if partial else None, sessions=n_sessions,
                           chunks=len(chunks), embedded=embedded,
                           reused=sum(1 for c in chunks if c["text"] not in fresh),
                           unreadable=unreadable,
                           partial=partial)

        old_pair = (old_m["rows"], old_m["vectors"]) if old_m is not None else None
        if old_m is not None and cur is not None and old_pair == (rows_name, vecs_name) \
                and old_m["files"] == files and old_m["work_dir"] == str(work_dir):
            dr.state = "unchanged"
            return dr                                        # 何も変わっていない

        validated: set[str] = set()
        if cur is not None:
            validated.update(old_pair)
        prev_pair = None
        if old_m is not None and old_m["previous"] is not None:
            prev_pair = (old_m["previous"]["rows"], old_m["previous"]["vectors"])
            if prev is not None:
                validated.update(prev_pair)
        new_pair = (rows_name, vecs_name)
        if cur is not None and old_pair != new_pair:
            keep_pair = old_pair            # 今の組が 1 世代前になる
        elif prev is not None and prev_pair != new_pair:
            keep_pair = prev_pair           # 今の組が壊れていた: 読めた 1 世代前を残す
        elif cur is not None:
            keep_pair = prev_pair           # 組は同じ（stat だけの更新）: 1 世代前はそのまま
        else:
            keep_pair = None
        previous = {"rows": keep_pair[0], "vectors": keep_pair[1]} if keep_pair else None
        manifest = {"version": VERSION, "dataset": name, "work_dir": str(work_dir),
                    "model": self.slug, "dims": self.dims, "recipe": self.recipe,
                    "chunker": CHUNKER_VERSION, "updated": time.time(),
                    "rows": rows_name, "vectors": vecs_name, "previous": previous,
                    "n_chunks": len(chunks), "files": files}
        try:
            shard.mkdir(parents=True, exist_ok=True)
            if rows_name not in validated or not (shard / rows_name).exists():
                cpaths.atomic_write_bytes(shard / rows_name, rows_bytes)
            if vecs_name not in validated or not (shard / vecs_name).exists():
                cpaths.atomic_write_bytes(shard / vecs_name, vec_bytes)
            cpaths.atomic_write_text(shard / "manifest.json",
                                     json.dumps(manifest, ensure_ascii=False, indent=2))
        except OSError as e:                                 # MountWriteError を含む
            dr.state, dr.reason = "skipped", "write"
            dr.detail = f"{type(e).__name__}: {e}"
            return dr                                        # GC はしない
        keep = {rows_name, vecs_name}
        if previous is not None:
            keep.update((previous["rows"], previous["vectors"]))
        with contextlib.suppress(OSError):
            for p in list(shard.iterdir()):
                garbage = p.name.endswith(".tmp") or (
                    (_ROWS_NAME.fullmatch(p.name) or _VECS_NAME.fullmatch(p.name))
                    and p.name not in keep)
                if garbage:
                    with contextlib.suppress(OSError):
                        p.unlink()
        return dr

    # ---- 検索

    def _load_for_search(self, name: str) -> tuple[_Data | None, list[IndexWarning]]:
        shard = _shard_dir(self.slug, name)
        for _attempt in range(2):
            mpath = shard / "manifest.json"
            if not mpath.exists():
                return None, []
            m = _read_manifest(shard, name, self.spec)
            if m is None:
                return None, [IndexWarning("shard-invalid", name)]
            data = _load_pair(shard, m["rows"], m["vectors"], self.dims)
            if data is None and m["previous"] is not None:
                data = _load_pair(shard, m["previous"]["rows"], m["previous"]["vectors"], self.dims)
            if data is not None:
                return data, []
        return None, [IndexWarning("shard-invalid", name)]

    def search(self, query: str, *, datasets=None, include_archived: bool = False,
               k: int = 20) -> tuple[list[RecallHit], list[IndexWarning]]:
        """問い合わせを埋め込み、内積と語彙シグナルを RRF で融合する。ロックは取らない。"""
        if not isinstance(query, str) or not query.strip():
            return [], []
        k = max(1, min(int(k), MAX_K))
        import config
        names = list(dict.fromkeys(datasets)) if datasets is not None else sorted(config.DATASETS)
        warnings: list[IndexWarning] = []
        blocks: list[tuple[str, _Data, np.ndarray]] = []
        for name in names:
            data, w = self._load_for_search(name)
            warnings.extend(w)
            if data is None or not data.chunks:
                continue
            keep = np.array([include_archived or not data.sessions[c["sid"]]["archived"]
                             for c in data.chunks], dtype=bool)
            if keep.any():
                blocks.append((name, data, np.nonzero(keep)[0]))
        if not blocks:
            return [], warnings
        qv = np.asarray(self.embedder.embed_query(query), dtype=np.float32).reshape(-1)
        if qv.shape != (self.dims,):
            raise RuntimeError(f"query embedding has shape {qv.shape}")
        terms = lexical_terms(query)
        refs: list[tuple[str, _Data, int]] = []
        mats = []
        lex_list: list[float] = []
        for name, data, idx in blocks:
            mats.append(data.vectors[idx])
            norm = data.norm_texts()
            for i in idx:
                refs.append((name, data, int(i)))
                lex_list.append(lexical_score(norm[i], terms))
        cos = np.concatenate(mats) @ qv
        lex = np.asarray(lex_list, dtype=np.float64)
        rank_cos = _rank_desc(cos)
        has_lex = lex > 0
        rank_lex = np.zeros(len(lex), dtype=np.int64)
        if has_lex.any():
            s = np.sort(lex[has_lex])
            rank_lex[has_lex] = 1 + (len(s) - np.searchsorted(s, lex[has_lex], side="right"))
        cand = set(np.argsort(-cos, kind="stable")[:_CANDIDATES].tolist())
        lex_order = [i for i in np.lexsort((-cos, -lex)).tolist() if has_lex[i]]
        cand.update(lex_order[:_CANDIDATES])
        best: dict[tuple[str, str, int], tuple[float, float, int]] = {}
        for i in cand:
            score = 1.0 / (_RRF_K + rank_cos[i])
            if has_lex[i]:
                score += 1.0 / (_RRF_K + rank_lex[i])
            name, data, j = refs[i]
            c = data.chunks[j]
            key = (name, c["sid"], c["idx"])
            cur = best.get(key)
            if cur is None or (score, float(cos[i])) > (cur[0], cur[1]):
                best[key] = (score, float(cos[i]), i)
        ordered = sorted(best.values(), key=lambda t: (-t[0], -t[1]))[:k]
        hits: list[RecallHit] = []
        for score, c_val, i in ordered:
            name, data, j = refs[i]
            c = data.chunks[j]
            meta = data.sessions[c["sid"]]
            hits.append(RecallHit(
                sid=c["sid"], sid8=chat_search.short_id(c["sid"]), idx=c["idx"], dataset=name,
                title=meta["title"], archived=meta["archived"], role=c["role"], score=score,
                cosine=c_val, lexical=float(lex[i]), snippet=chat_search._clip(c["text"], SNIPPET_CHARS),
                updated=float(meta["updated"])))
        return hits, warnings

    def stale_summary(self, datasets=None) -> dict[str, int]:
        return _stale_summary(self.spec, datasets)

    def recall(self, query: str, *, datasets=None, include_archived: bool = False, k: int = 20,
               update: bool = False, budget_s: float | None = None,
               blocking: bool = False) -> RecallResult:
        """（必要なら差分更新してから）検索し、索引の遅れを添える。"""
        warnings: list[IndexWarning] = []
        updated = False
        if update:
            rep = self.update(datasets, blocking=blocking, budget_s=budget_s)
            updated = rep.ran
            warnings.extend(rep.warnings())
        hits, w = self.search(query, datasets=datasets, include_archived=include_archived, k=k)
        warnings.extend(w)
        return RecallResult(hits, updated, self.stale_summary(datasets), warnings)

    def clear(self, datasets=None) -> int:
        return clear(datasets, model_id=self.slug)


# ---------------------------------------------------------------- ロック・読むだけの API


@contextlib.contextmanager
def _blocking_lock(path: Path):
    stack = contextlib.ExitStack()
    try:
        stack.enter_context(filelock.exclusive_lock(path))   # 取得だけを包む
    except filelock.LockTimeout as e:
        raise IndexLocked(str(e)) from e
    with stack:
        yield


def _stale_summary(spec: _Spec, datasets) -> dict[str, int]:
    import config
    names = list(dict.fromkeys(datasets)) if datasets is not None else sorted(config.DATASETS)
    out: dict[str, int] = {}
    for name in names:
        where = _locate(name)
        if where.work_dir is None:
            continue
        try:
            scan = _scan(where.work_dir / "chat_sessions")
        except OSError:
            continue
        m = _read_manifest(_shard_dir(spec.slug, name), name, spec)
        n = _stale_count(scan, m, where.work_dir)
        if n:
            out[name] = n
    return out


def stale_summary(datasets=None, *, model_id: str | None = None) -> dict[str, int]:
    """届くデータセットごとの「保存済みのチャットに対して索引が遅れているファイルの数」（0 は省く）。

    stat と manifest だけを見る（推論も rows の読取もしない）。"""
    return _stale_summary(_spec_for(model_id or load_prefs().model_id), datasets)


def _dir_bytes(d: Path) -> int:
    total = 0
    with contextlib.suppress(OSError), os.scandir(d) as it:
        for e in it:
            with contextlib.suppress(OSError):
                if e.is_file():
                    total += e.stat().st_size
    return total


def status(datasets=None, *, model_id: str | None = None) -> IndexStatus:
    """索引の状況（推論なし）。登録済みのデータセットだけを並べる。"""
    import config
    prefs = load_prefs()
    mid = model_id or prefs.model_id
    spec = _spec_for(mid)
    model = embed_model.CATALOG.get(mid)
    if model is not None:
        r = embed_model.readiness(mid)
        deps, state = r.deps_missing, r.model_state
    else:
        deps, state = embed_model.missing_packages(), "missing"
    names = list(dict.fromkeys(datasets)) if datasets is not None else sorted(config.DATASETS)
    stale = _stale_summary(spec, names)
    shards: list[ShardStatus] = []
    for name in names:
        sd = _shard_dir(mid, name)
        st = ShardStatus(name, stale=stale.get(name, 0))
        m = _read_manifest(sd, name, spec)
        if m is not None:
            st.sessions = sum(1 for v in m["files"].values() if v["indexed"])
            st.chunks = m["n_chunks"]
            st.updated = float(m["updated"])
        if sd.is_dir():
            st.bytes = _dir_bytes(sd)
        shards.append(st)
    updates = [s.updated for s in shards if s.updated is not None]
    return IndexStatus(
        enabled=prefs.enabled,
        ready=prefs.enabled and not deps and state == "installed",
        model=mid, deps_missing=list(deps), model_state=state,
        slug_dir=str(_slug_dir(mid)), dims=model.dims if model is not None else spec.dims,
        datasets=shards, total_chunks=sum(s.chunks for s in shards),
        total_bytes=sum(s.bytes for s in shards),
        last_update=max(updates) if updates else None, stale=stale)


def dataset_of_session(sid_prefix, *, model_id: str | None = None) -> str | None:
    """索引にあるチャットの sid の前方一致でデータセットを引く（1 件に決まるときだけ）。"""
    prefix = str(sid_prefix).strip().lower()
    if _SID_PREFIX.fullmatch(prefix) is None:
        return None
    import config
    spec = _spec_for(model_id or load_prefs().model_id)
    found: set[tuple[str, str]] = set()
    for name in sorted(config.DATASETS):
        m = _read_manifest(_shard_dir(spec.slug, name), name, spec)
        if m is None:
            continue
        for stem, ent in m["files"].items():
            if ent["indexed"] and stem.startswith(prefix):
                found.add((name, stem))
    return next(iter(found))[0] if len(found) == 1 else None


def read_message(dataset: str, sid: str, idx: int) -> str | None:
    """ディスクの保存済みのチャットから 1 メッセージの本文（検索の対象の部分）。never raise。"""
    try:
        if not isinstance(sid, str) or _SID_SAFE.fullmatch(sid) is None:
            return None
        if not _is_int(idx) or idx < 0:
            return None
        import dataset_config
        wd = dataset_config.get_work_dir(dataset, create=False)
        sess = chat_store.read_session_file(wd / "chat_sessions" / f"{sid}.json")
        if sess is None or sess.id != sid or idx >= len(sess.messages):
            return None
        return chat_search.searchable_text(sess.messages[idx])
    except Exception:  # noqa: BLE001 — never raise
        return None


def clear(datasets=None, *, model_id: str | None = None) -> int:
    """索引を消す。None なら全 slug、指定があれば今の slug の該当データセットだけ。→ 消した shard の数。"""
    root = lb_paths.chat_index_dir()
    if not root.is_dir():
        return 0
    n = 0
    if datasets is None:
        for d in sorted(root.iterdir()):
            if not d.is_dir() or _SLUG.fullmatch(d.name) is None:
                continue
            with _blocking_lock(root / f"{d.name}.lock"):
                with contextlib.suppress(OSError):
                    n += sum(1 for p in d.iterdir() if p.is_dir() and _DSKEY.fullmatch(p.name))
                _rmtree(d)
    else:
        slug = model_id or load_prefs().model_id
        with _blocking_lock(_lock_file(slug)):
            for name in dict.fromkeys(datasets):
                sd = _shard_dir(slug, name)
                if sd.exists():
                    _rmtree(sd)
                    n += 1
    return n
