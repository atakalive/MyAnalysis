"""チャットの関連検索の埋め込みモデル: catalog・導入・削除・ONNX 推論（Qt 非依存。Issue #125）。

モデルはリポジトリに同梱しない。Hugging Face の固定した rev の URL から取得し、コードに固定した
sha256 と大きさで照合してから、PC ローカルの ``install_root()``（``~/.myanalysis/embeddings``）へ
置く。サーバーの申告（Content-Length など）は信用しない。文章は返さない（状態とエラーの種類と
元のメッセージだけ。文は GUI と CLI が作る）。ui_prefs は読まない（有効・無効と選択は
``llm_bridge.chat_index`` が持つ）。

``<root>`` の中身:

- ``<id>/``: モデルのディレクトリ（``model.onnx``・``tokenizer.json``・``installed.json``）。
  ``installed.json`` は最後に書くので、それが無い・catalog と合わないディレクトリは壊れている扱い。
- ``<id>.tmp-<token>/``: 導入 1 回ごとの作業ディレクトリと、削除の途中のディレクトリ。
  ``STALE_WORKDIR_S`` より古いものは次の導入が消す。
- ``install.lock``: 導入・削除の置き換えを直列にするロック。誰も消さない。

依存（``onnxruntime``・``tokenizers``）は任意で、``requirements.txt`` に無い。関数の中で遅延
import する。numpy（必須依存）も関数の中で import する（conftest がこのモジュールを全テストで
import するため）。

呼び出し側（GUI・索引・テスト）は ``embed_model.install_root()`` と属性で引くこと
（``from … import install_root`` だと conftest の差し替えが効かない）。
"""
from __future__ import annotations

import contextlib
import hashlib
import http.client
import importlib
import importlib.util
import json
import os
import queue
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from common.filelock import exclusive_lock
from common.paths import atomic_write_text

# ---------------------------------------------------------------- catalog（固定値）


@dataclass(frozen=True)
class ModelFile:
    url: str          # 固定した rev の URL
    sha256: str       # 照合に使う値（サーバーの申告ではなくこれと照合する）
    size: int         # バイト数
    name: str         # 保存名（model.onnx / tokenizer.json）


@dataclass(frozen=True)
class EmbedModel:
    id: str
    label_key: str                 # i18n のキー（文言の登録は GUI 側）
    dims: int
    max_tokens: int                # 推論時の切り詰め
    query_prefix: str
    document_prefix: str
    pooling: str                   # "mean"
    developer: str
    homepage: str
    source: str
    distribution: str
    license: str
    license_url: str
    files: tuple[ModelFile, ...]

    @property
    def recipe(self) -> str:
        """文書側の埋め込みを決める値の指紋。変われば保存済みのベクトルは使えない。

        query_prefix は保存済みのベクトルに影響しないので入れない。"""
        spec = {
            "files": {f.name: f.sha256 for f in self.files},
            "document_prefix": self.document_prefix,
            "pooling": self.pooling,
            "max_tokens": self.max_tokens,
            "dims": self.dims,
        }
        text = json.dumps(spec, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


_HF = "https://huggingface.co"
# 版を上げるときは、rev と sha256 と大きさを書き換えるコミットにする（2026-11-05 に API で確認）。
_RURI_ONNX_REV = "1b41a27f31213fc55a9c4653c067770e39406408"   # onnx-community/ruri-v3-30m-ONNX
_RURI_REV = "24899e5de370b56d179604a007c0d727bf144504"        # cl-nagoya/ruri-v3-30m
_E5_REV = "614241f622f53c4eeff9890bdc4f31cfecc418b3"          # intfloat/multilingual-e5-small

APACHE_LICENSE_URL = "https://www.apache.org/licenses/LICENSE-2.0"
MIT_LICENSE_URL = "https://opensource.org/license/mit"

CATALOG: dict[str, EmbedModel] = {
    "ruri-v3-30m": EmbedModel(
        id="ruri-v3-30m",
        label_key="chat.recall.model.ruri_v3_30m",
        dims=256,
        max_tokens=1024,
        query_prefix="検索クエリ: ",
        document_prefix="検索文書: ",
        pooling="mean",
        developer="Nagoya University (cl-nagoya)",
        homepage=f"{_HF}/cl-nagoya/ruri-v3-30m",
        source=f"{_HF}/cl-nagoya/ruri-v3-30m",
        distribution=f"{_HF}/onnx-community/ruri-v3-30m-ONNX",
        license="Apache-2.0",
        license_url=APACHE_LICENSE_URL,
        files=(
            ModelFile(
                f"{_HF}/onnx-community/ruri-v3-30m-ONNX/resolve/{_RURI_ONNX_REV}/onnx/model.onnx",
                "5ffd75c4d39c2d783efc46f45c1b5c9b0e19699e2b2b60396ae45dbd7d8ef869",
                147_020_821, "model.onnx"),
            ModelFile(
                f"{_HF}/cl-nagoya/ruri-v3-30m/resolve/{_RURI_REV}/tokenizer.json",
                "0a94ac9a0a02c067bdef25b72ae9f4ee33f48f552e55988d444f6d25eeb1d062",
                6_724_873, "tokenizer.json"),
        ),
    ),
    "multilingual-e5-small": EmbedModel(
        id="multilingual-e5-small",
        label_key="chat.recall.model.multilingual_e5_small",
        dims=384,
        max_tokens=512,
        query_prefix="query: ",
        document_prefix="passage: ",
        pooling="mean",
        developer="Microsoft (intfloat)",
        homepage=f"{_HF}/intfloat/multilingual-e5-small",
        source=f"{_HF}/intfloat/multilingual-e5-small",
        distribution=f"{_HF}/intfloat/multilingual-e5-small",
        license="MIT",
        license_url=MIT_LICENSE_URL,
        files=(
            ModelFile(
                f"{_HF}/intfloat/multilingual-e5-small/resolve/{_E5_REV}/onnx/model.onnx",
                "ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665",
                470_268_510, "model.onnx"),
            ModelFile(
                f"{_HF}/intfloat/multilingual-e5-small/resolve/{_E5_REV}/onnx/tokenizer.json",
                "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
                17_082_730, "tokenizer.json"),
        ),
    ),
}
DEFAULT_MODEL = "ruri-v3-30m"
REQUIRED_PACKAGES = (("onnxruntime", "onnxruntime"), ("tokenizers", "tokenizers"))   # (import 名, pip 名)
# 設定のウインドウが出所として見せる、pip 名ごとの配布元のページ（Issue #126）。
PACKAGE_PAGES = {
    "onnxruntime": "https://pypi.org/project/onnxruntime/",
    "tokenizers": "https://pypi.org/project/tokenizers/",
}

ERRORS = ("network", "integrity", "cancelled", "io", "in_use", "pip")

FETCH_TIMEOUT_S = 30           # urllib の 1 回の読み込みごとのタイムアウト
_CHUNK = 64 * 1024
BATCH = 16
STALE_WORKDIR_S = 24 * 3600
# 似た対（0 と 1）が遠い対（0・1 と 2）より近いことを確かめる固定の 3 文。
SELFCHECK_TEXTS = ("ピーク位置のずれを補正する", "ピークの位置ずれを直す方法", "週末は山に登って紅葉を見た")


def install_root() -> Path:
    """導入先（PC ローカル）。テストは conftest で tmp に向ける。"""
    return Path.home() / ".myanalysis" / "embeddings"


# ---------------------------------------------------------------- 結果の型


@dataclass(frozen=True)
class InstallResult:
    ok: bool
    error: str | None = None       # ERRORS のどれか
    message: str = ""              # 元のメッセージ（表示の文ではない）


@dataclass(frozen=True)
class Status:
    state: str                     # missing / installed / broken
    size: int = 0                  # installed のときのファイルの合計


@dataclass(frozen=True)
class Readiness:
    deps_missing: list[str]        # pip 名
    model_state: str               # Status.state
    model: EmbedModel


@dataclass(frozen=True)
class CheckInfo:
    ok: bool
    dims: int
    latency_ms: float
    error: str | None


class NotReady(RuntimeError):
    """依存かモデルが無い・閉じた後で、埋め込めない。"""


class Cancelled(Exception):
    """埋め込みの途中で cancel が立った。"""


class _Fail(Exception):
    def __init__(self, kind: str, message: str = ""):
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------- 状態


def _read_installed(d: Path) -> dict | None:
    try:
        data = json.loads((d / "installed.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _installed_info(model: EmbedModel) -> dict:
    return {"model": model.id,
            "files": {f.name: {"sha256": f.sha256, "size": f.size} for f in model.files}}


def _state_at(root: Path, model: EmbedModel) -> Status:
    """渡された root での導入状態（ハッシュは取り直さない）。"""
    d = Path(root) / model.id
    if not d.exists():
        return Status("missing")
    if not d.is_dir():
        return Status("broken")
    data = _read_installed(d)
    want = _installed_info(model)
    if data is None or data.get("model") != want["model"] or data.get("files") != want["files"]:
        return Status("broken")
    total = 0
    for f in model.files:
        try:
            size = (d / f.name).stat().st_size
        except OSError:
            return Status("broken")
        if size != f.size:
            return Status("broken")
        total += size
    return Status("installed", total)


def status(model_id: str) -> Status:
    """``install_root()`` での導入状態。未知の id は KeyError。"""
    return _state_at(install_root(), CATALOG[model_id])


# ---------------------------------------------------------------- 取得（テストが差し替える口）


def _urlopen(url: str, timeout: float):
    """テストが差し替える取得口。プロキシは urllib の既定（環境変数・Windows のレジストリ）。"""
    return urllib.request.urlopen(url, timeout=timeout)   # https の固定 URL だけ


def _rename(src: Path, dst: Path) -> None:
    """テストが差し替える rename（Windows で開かれているファイルの検出に使う）。"""
    os.replace(src, dst)


def _lock_path(root: Path) -> Path:
    return root / "install.lock"


def _download(f: ModelFile, dest: Path, done: list[int], total: int,
              progress: Callable[[int, int], None], cancelled: Callable[[], bool]) -> None:
    """一時ファイルに流しながら sha256 を取り、固定の大きさ・sha256 と照合する。"""
    h = hashlib.sha256()
    got = 0
    try:
        out = open(dest, "wb")  # noqa: SIM115 — 下の with で閉じる
    except OSError as e:
        raise _Fail("io", f"{type(e).__name__}: {e}") from e
    with out:
        try:
            resp = _urlopen(f.url, FETCH_TIMEOUT_S)
        except (OSError, http.client.HTTPException) as e:   # URLError・タイムアウトは OSError
            raise _Fail("network", f"{type(e).__name__}: {e}") from e
        with resp:
            while True:
                if cancelled():
                    raise _Fail("cancelled")
                try:
                    chunk = resp.read(_CHUNK)
                except (OSError, http.client.HTTPException) as e:
                    raise _Fail("network", f"{type(e).__name__}: {e}") from e
                if not chunk:
                    break
                got += len(chunk)
                if got > f.size:
                    raise _Fail("integrity", f"{f.name}: more than {f.size} bytes")
                h.update(chunk)
                try:
                    out.write(chunk)
                except OSError as e:
                    raise _Fail("io", f"{type(e).__name__}: {e}") from e
                done[0] += len(chunk)
                progress(done[0], total)
    if got != f.size:
        raise _Fail("integrity", f"{f.name}: size {got} != {f.size}")
    digest = h.hexdigest()
    if digest != f.sha256:
        raise _Fail("integrity", f"{f.name}: sha256 {digest}")


def _clean_stale_workdirs(root: Path) -> None:
    """古い作業ディレクトリ・削除の残骸を消す（install.lock を持ったまま呼ぶ）。"""
    now = time.time()
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    for p in entries:
        if ".tmp-" not in p.name or not p.is_dir():
            continue
        try:
            old = now - p.stat().st_mtime > STALE_WORKDIR_S
        except OSError:
            continue
        if old:
            shutil.rmtree(p, ignore_errors=True)


def _discard(root: Path, d: Path) -> None:
    """モデルのディレクトリを作業名へ rename してから消す（install.lock を持ったまま呼ぶ）。

    中のファイルが開かれていると Windows では rename が PermissionError になるので、何も変えずに
    in_use にする（半分消えた状態を作らない）。開いていなければ通るので、使用中かどうかの確かな
    判定ではない（GUI は先に埋め込み器を閉じる）。"""
    trash = root / f"{d.name}.tmp-{uuid.uuid4().hex}"
    try:
        _rename(d, trash)
    except PermissionError as e:
        raise _Fail("in_use", f"{type(e).__name__}: {e}") from e
    except OSError as e:
        raise _Fail("io", f"{type(e).__name__}: {e}") from e
    shutil.rmtree(trash, ignore_errors=True)   # 残骸は次の導入の掃除で消える


# ---------------------------------------------------------------- 導入・削除


def install(model_id: str, progress: Callable[[int, int], None],
            cancelled: Callable[[], bool]) -> InstallResult:
    """固定 URL から取得し、大きさと sha256 を照合して置く。never raise。

    ``progress(done_bytes, total_bytes)`` は呼び出したスレッドで呼ぶ。"""
    model = CATALOG.get(model_id)
    if model is None:
        return InstallResult(False, "io", f"unknown model: {model_id}")
    root = install_root()
    work = root / f"{model.id}.tmp-{uuid.uuid4().hex}"
    try:
        if _state_at(root, model).state == "installed":
            return InstallResult(True)
        root.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(_lock_path(root)):
            _clean_stale_workdirs(root)
            work.mkdir()
        total = sum(f.size for f in model.files)
        done = [0]
        for f in model.files:                                    # 取得はロックの外
            _download(f, work / f.name, done, total, progress, cancelled)
        if cancelled():
            raise _Fail("cancelled")
        with exclusive_lock(_lock_path(root)):
            if _state_at(root, model).state == "installed":     # ほかのプロセスが先に入れた
                return InstallResult(True)
            dest = root / model.id
            if dest.exists() or dest.is_symlink():
                _discard(root, dest)                             # 壊れたものを置き換える
            try:
                _rename(work, dest)
            except OSError as e:
                raise _Fail("io", f"{type(e).__name__}: {e}") from e
            atomic_write_text(dest / "installed.json",
                              json.dumps(_installed_info(model), ensure_ascii=False, indent=2))
        return InstallResult(True)
    except _Fail as e:
        return InstallResult(False, e.kind, str(e))
    except Exception as e:  # noqa: BLE001 — never raise
        return InstallResult(False, "io", f"{type(e).__name__}: {e}")
    finally:
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)


def uninstall(model_id: str) -> InstallResult:
    """モデルのディレクトリを消す。never raise。使用中（Windows）なら何も変えずに in_use。"""
    model = CATALOG.get(model_id)
    if model is None:
        return InstallResult(False, "io", f"unknown model: {model_id}")
    root = install_root()
    if not root.exists():
        return InstallResult(True)
    try:
        with exclusive_lock(_lock_path(root)):
            d = root / model.id
            if d.exists() or d.is_symlink():
                _discard(root, d)
        return InstallResult(True)
    except _Fail as e:
        return InstallResult(False, e.kind, str(e))
    except Exception as e:  # noqa: BLE001 — never raise
        return InstallResult(False, "io", f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------- 依存パッケージ


def _find_spec(name: str):
    """テストが差し替える import 可能性の検査。"""
    return importlib.util.find_spec(name)


def missing_packages() -> list[str]:
    """import できない任意依存の pip 名。"""
    out: list[str] = []
    for import_name, pip_name in REQUIRED_PACKAGES:
        try:
            spec = _find_spec(import_name)
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            out.append(pip_name)
    return out


def pip_install_argv() -> list[str]:
    from common import self_update
    return [self_update.python_for_pip(), "-m", "pip", "install",
            *(pip for _imp, pip in REQUIRED_PACKAGES),
            "--disable-pip-version-check", "--no-input"]


def _popen(argv: list[str], **kw) -> subprocess.Popen:
    """テストが差し替える起動口。"""
    return subprocess.Popen(argv, **kw)   # 固定の argv


def _terminate(p) -> None:
    with contextlib.suppress(OSError):
        p.terminate()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            p.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            p.wait(timeout=5)


def install_packages(progress: Callable[[str], None],
                     cancelled: Callable[[], bool]) -> InstallResult:
    from common.paths import repo_root
    from installer.core import mutation_lock, check_children
    try:
        with mutation_lock(repo_root()):
            check_children()
            return _install_packages(progress, cancelled)
    except Exception as exc:
        return InstallResult(False, "pip", str(exc))


def install_packages_worker(progress, cancelled):
    from common.paths import repo_root
    from installer.tasks import monitor
    try:
        return InstallResult(**monitor("optional-pip", repo_root(), progress=progress, cancelled=cancelled))
    except Exception as exc:
        return InstallResult(False, "pip", str(exc))


def _install_packages(progress: Callable[[str], None],
                      cancelled: Callable[[], bool]) -> InstallResult:
    """``pip install onnxruntime tokenizers`` を実行する。never raise。

    出力は 1 行ずつ ``progress(line)`` に渡す（呼び出したスレッドで呼ぶ）。Windows では読み込み
    済みの onnxruntime の DLL を置き換えられないので、更新は次回の起動から効く。"""
    from common import proc, self_update
    if os.environ.get("MYANALYSIS_MUTATION_WORKER") == "1":
        from installer.core import run, SetupError
        try:
            text = run(pip_install_argv(), timeout=self_update.PIP_TIMEOUT_S,
                       env=dict(os.environ, PIP_NO_INPUT="1"), cancelled=cancelled,
                       progress=progress)
            return InstallResult(True, message=text[-8192:])
        except SetupError as exc:
            return InstallResult(False, "cancelled" if cancelled() else "pip", str(exc))
    tail: deque[str] = deque(maxlen=20)
    try:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PIP_NO_INPUT"] = "1"
        try:
            p = _popen(pip_install_argv(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                       env=env, **proc.no_window_kwargs())
        except (OSError, ValueError) as e:
            return InstallResult(False, "pip", f"{type(e).__name__}: {e}")
        lines: queue.Queue[str | None] = queue.Queue()

        def _reader():
            try:
                for line in p.stdout:
                    lines.put(line.rstrip("\r\n"))
            except (OSError, ValueError):
                pass
            finally:
                lines.put(None)

        t = threading.Thread(target=_reader, name="pip-reader", daemon=True)
        t.start()

        def _drain() -> None:
            while True:
                try:
                    line = lines.get_nowait()
                except queue.Empty:
                    return
                if line is not None:
                    tail.append(line)
                    progress(line)

        deadline = time.monotonic() + self_update.PIP_TIMEOUT_S
        while p.poll() is None:
            _drain()
            if cancelled():
                _terminate(p)
                _drain()
                return InstallResult(False, "cancelled", "\n".join(tail))
            if time.monotonic() > deadline:
                _terminate(p)
                _drain()
                return InstallResult(False, "pip", "timeout")
            time.sleep(0.1)
        t.join(timeout=5)
        _drain()
        if p.returncode != 0:
            return InstallResult(False, "pip", "\n".join(tail))
        importlib.invalidate_caches()          # 同じプロセスの find_spec に見えるように
        return InstallResult(True, message="\n".join(tail))
    except Exception as e:  # noqa: BLE001 — never raise
        return InstallResult(False, "pip", f"{type(e).__name__}: {e}")


def readiness(model_id: str) -> Readiness:
    """依存とモデルの状態（ネットワーク・推論なし）。未知の id は KeyError。"""
    model = CATALOG[model_id]
    return Readiness(missing_packages(), status(model_id).state, model)


# ---------------------------------------------------------------- 推論


def _import_deps():
    """テストが差し替える任意依存の import。→ (onnxruntime, tokenizers.Tokenizer)"""
    import onnxruntime
    from tokenizers import Tokenizer
    return onnxruntime, Tokenizer


_KNOWN_INPUTS = ("input_ids", "attention_mask", "token_type_ids")
_GPU_PROVIDERS = ("CUDAExecutionProvider", "DmlExecutionProvider")


def _providers(ort) -> list[str]:
    try:
        avail = list(ort.get_available_providers())
    except Exception:  # noqa: BLE001
        avail = []
    return [p for p in _GPU_PROVIDERS if p in avail] + ["CPUExecutionProvider"]


def _pad_of(tok) -> tuple[int, str]:
    pad = getattr(tok, "padding", None)
    if isinstance(pad, dict) and isinstance(pad.get("pad_id"), int) \
            and isinstance(pad.get("pad_token"), str):
        return pad["pad_id"], pad["pad_token"]
    for t in ("<pad>", "[PAD]"):
        i = tok.token_to_id(t)
        if isinstance(i, int):
            return i, t
    return 0, "<pad>"


class OnnxEmbedder:
    """ONNX Runtime（CPU）で文を埋め込む。``llm_bridge.chat_index`` の Embedder を満たす。

    ``run`` は GIL を離すので、デーモンスレッドから呼べば GUI は止まらない。"""

    def __init__(self, model: EmbedModel, root: Path, *, threads: int | None = None):
        self.model = model
        self.model_id = model.id
        self.dims = model.dims
        self.recipe = model.recipe
        missing = missing_packages()
        if missing:
            raise NotReady("missing: " + ", ".join(missing))
        st = _state_at(Path(root), model)
        if st.state != "installed":
            raise NotReady(f"model {model.id}: {st.state}")
        try:
            ort, tokenizer_cls = _import_deps()
        except Exception as e:       # Windows の DLL の読み込みの失敗など
            raise NotReady(f"import failed: {type(e).__name__}: {e}") from e
        d = Path(root) / model.id
        tok = tokenizer_cls.from_file(str(d / "tokenizer.json"))
        tok.enable_truncation(max_length=model.max_tokens)
        pad_id, pad_token = _pad_of(tok)
        tok.enable_padding(pad_id=pad_id, pad_token=pad_token)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads or max(1, (os.cpu_count() or 2) - 1)
        opts.inter_op_num_threads = 1
        providers = _providers(ort)
        if "DmlExecutionProvider" in providers:
            opts.enable_mem_pattern = False        # DirectML の要件
        session = ort.InferenceSession(str(d / "model.onnx"), sess_options=opts,
                                       providers=providers)
        inputs = [i.name for i in session.get_inputs()]
        unknown = [n for n in inputs if n not in _KNOWN_INPUTS]
        if unknown or "input_ids" not in inputs:
            raise RuntimeError(f"unsupported model input: {', '.join(unknown or inputs)}")
        outputs = [o.name for o in session.get_outputs()]
        if not outputs:
            raise RuntimeError("model has no output")
        self._inputs = inputs
        self._output = "last_hidden_state" if "last_hidden_state" in outputs else outputs[0]
        self._session = session
        self._tok = tok
        self._tok_lock = threading.Lock()

    def _live(self):
        sess, tok = self._session, self._tok
        if sess is None or tok is None:
            raise NotReady("closed")
        return sess, tok

    def _embed(self, texts: list[str], progress, cancel):
        import numpy as np
        n = len(texts)
        out = np.zeros((n, self.dims), dtype=np.float32)
        if n == 0:
            return out
        order = sorted(range(n), key=lambda i: len(texts[i]))   # 長さ順で padding を減らす
        done = 0
        for start in range(0, n, BATCH):
            if cancel is not None and cancel():
                raise Cancelled()
            sess, tok = self._live()
            idx = order[start:start + BATCH]
            with self._tok_lock:
                encs = tok.encode_batch([texts[i] for i in idx])
            ids = np.asarray([e.ids for e in encs], dtype=np.int64)
            mask = np.asarray([e.attention_mask for e in encs], dtype=np.int64)
            feed = {}
            for name in self._inputs:
                if name == "input_ids":
                    feed[name] = ids
                elif name == "attention_mask":
                    feed[name] = mask
                else:                                            # token_type_ids
                    feed[name] = np.zeros_like(ids)
            h = np.asarray(sess.run([self._output], feed)[0], dtype=np.float32)
            if h.ndim != 3 or h.shape[0] != len(idx) or h.shape[1] != ids.shape[1] \
                    or h.shape[2] != self.dims:
                raise RuntimeError(f"unexpected output shape {h.shape}")
            m = mask[:, :, None] > 0
            summed = np.where(m, h, 0.0).sum(axis=1)
            count = np.maximum(m.sum(axis=1).astype(np.float32), 1e-9)
            v = summed / count
            v = v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12)
            if not np.isfinite(v).all():
                raise RuntimeError("non-finite embedding")
            out[idx] = v
            done += len(idx)
            if progress is not None:
                progress(done, n)
        return out

    def embed_documents(self, texts, *, progress=None, cancel=None):
        """(n, dims) float32・L2 正規化。document_prefix を付ける。"""
        prefix = self.model.document_prefix
        return self._embed([prefix + t for t in texts], progress, cancel)

    def embed_query(self, text):
        """(dims,) float32・L2 正規化。query_prefix を付ける。"""
        return self._embed([self.model.query_prefix + text], None, None)[0]

    def selfcheck(self) -> CheckInfo:
        """固定の 3 文で「似た対が遠い対より近い」ことを確かめる。never raise。"""
        try:
            v = self.embed_documents(list(SELFCHECK_TEXTS))
            ab, ac, bc = float(v[0] @ v[1]), float(v[0] @ v[2]), float(v[1] @ v[2])
            t0 = time.perf_counter()
            q = self.embed_query(SELFCHECK_TEXTS[0])
            ms = (time.perf_counter() - t0) * 1000.0
            dims = int(v.shape[1])
            if q.shape != (self.dims,):
                return CheckInfo(False, dims, ms, f"query shape {q.shape}")
            if ab > ac and ab > bc:
                return CheckInfo(True, dims, ms, None)
            return CheckInfo(False, dims, ms, f"order: ab={ab:.3f} ac={ac:.3f} bc={bc:.3f}")
        except Exception as e:  # noqa: BLE001 — never raise
            return CheckInfo(False, self.dims, 0.0, f"{type(e).__name__}: {e}")

    def close(self) -> None:
        """session と tokenizer を手放す（削除の前）。以後の埋め込みは NotReady。"""
        self._session = None
        self._tok = None
