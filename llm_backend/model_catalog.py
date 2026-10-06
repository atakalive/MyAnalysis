"""バックエンド状況ウィンドウの「モデル取得」（Qt 非依存）。

インストール済みのツール自身が知っているモデル ID を読み出し、``models.toml`` の
``[<section>].model_choices`` へ**新規分だけを追記**する。推論は走らせず、取得した ID を
試す送信もしない（トークンを使うのはユーザーが押す疎通確認だけ）。

取得元（いずれも課金なし）:

- claude-vscode / claude-cli: エンジンを stream-json で起動し、``initialize`` の
  control_request だけを送って応答の ``models[].value`` を読む（``user`` メッセージは
  送らないのでターンは走らない）。
- pi: ``pi --list-models`` のうち ``PI_PROVIDERS`` の provider の行。
- codex: ``$CODEX_HOME/models_cache.json``（既定 ``~/.codex``）を読むだけ。codex は起動しない。

文言は持たない（preflight と同じ規約）。``detail`` は中立な英語の事実、説明は
``notes`` の ``(i18n キー, params)`` で返し、呼び出し側が翻訳する。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from llm_backend import engines, preflight
from llm_backend.codex import codex_home

SUPPORTED: tuple[str, ...] = ("claude-vscode", "claude-cli", "pi", "codex")


@dataclass(frozen=True)
class ModelList:
    engine_id: str
    state: str                                   # "ok" | "missing" | "unknown"
    models: tuple[str, ...] = ()                 # 追記候補の ID（取得元の順・重複なし・空白除去済み）
    source: str = ""                             # 取得元（中立な事実）
    detail: str = ""                             # 失敗理由・補足（中立な英語の事実。ローカライズしない）
    aliases: tuple[tuple[str, str], ...] = ()    # claude だけ: (value, resolvedModel)
    fetched_at: str = ""                         # codex だけ: models_cache.json の fetched_at
    notes: tuple[tuple[str, dict], ...] = ()     # (i18n キー, params)。EngineStatus.notes と同じ規約


@dataclass(frozen=True)
class AppendOutcome:
    added: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()
    written: bool = False


def _engine_settings(engine_id: str) -> dict:
    """チャットと同じ経路で解いたそのエンジンの設定（vscode/cli の ``bin`` を含む）。"""
    return engines.session_settings(engines.engine_by_id(engine_id), "", "")


def fetch_models(engine_id: str) -> ModelList:
    """``engine_id`` のツールが知っているモデル ID を読む。例外は外へ出さない。"""
    try:
        if engine_id not in SUPPORTED:
            return ModelList(engine_id, "unknown", detail=f"unsupported engine: {engine_id}")
        if engine_id == "codex":
            return _fetch_codex(engine_id)
        settings = _engine_settings(engine_id)
        if engine_id == "pi":
            return _fetch_pi(engine_id, settings)
        return _fetch_claude(engine_id, settings)
    except Exception as e:
        return ModelList(engine_id, "unknown", detail=f"{type(e).__name__}: {e}")


def _fetch_claude(engine_id: str, settings: dict) -> ModelList:
    from llm_backend.claude_code import ClaudeCodeBackend

    be = ClaudeCodeBackend(settings)
    source = be._discover_binary()
    body = be.list_models()
    raw = body.get("models")
    if not isinstance(raw, list):
        return ModelList(
            engine_id, "unknown", source=source,
            detail="initialize response has no models list",
            notes=(("backend.models.claude_no_models", {}),),
        )
    models: list[str] = []
    aliases: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if not isinstance(value, str):
            continue
        value = value.strip()
        # "default" は除外する（モデル欄の空欄が既定を意味するため）。
        if not value or value == "default" or value in models:
            continue
        models.append(value)
        resolved = item.get("resolvedModel")
        if isinstance(resolved, str) and resolved.strip() and resolved.strip() != value:
            aliases.append((value, resolved.strip()))
    return ModelList(
        engine_id, "ok", models=tuple(models), source=source, aliases=tuple(aliases)
    )


def _fetch_pi(engine_id: str, settings: dict) -> ModelList:
    prov = settings.get("provider")
    prov = prov.strip() if isinstance(prov, str) else ""
    if prov and prov not in engines.PI_PROVIDERS:
        return ModelList(
            engine_id, "unknown",
            detail=f"provider {prov} is not in PI_PROVIDERS",
            notes=(("backend.models.pi_provider_unsupported",
                    {"provider": prov, "allowed": " / ".join(engines.PI_PROVIDERS)}),),
        )
    allowed = (prov,) if prov else engines.PI_PROVIDERS
    bin_name = settings.get("bin", "pi")
    pi_bin = preflight._which(bin_name)
    if pi_bin is None:
        return ModelList(engine_id, "missing", detail=f"pi not found: {bin_name!r}")
    r = preflight._run([pi_bin, "--list-models"], preflight._PROBE_TIMEOUT)
    if r is None:
        return ModelList(
            engine_id, "unknown", detail="pi --list-models timed out or could not start"
        )
    if r[0] != 0:
        detail = f"pi --list-models exited with code {r[0]}"
        first = next((ln.strip() for ln in (r[1] or "").splitlines() if ln.strip()), "")
        if first:
            detail += ": " + first[:200]
        return ModelList(engine_id, "unknown", detail=detail)
    models: list[str] = []
    seen_providers: set[str] = set()
    for provider, model in preflight._parse_list_models(r[1]):
        if provider not in allowed:
            continue
        seen_providers.add(provider)
        if model not in models:
            models.append(model)
    return ModelList(
        engine_id, "ok", models=tuple(models),
        source=f"{pi_bin} --list-models",
        detail=", ".join(p for p in allowed if p in seen_providers),
    )


def _codex_cache_path() -> Path:
    return codex_home() / "models_cache.json"


def _priority(item: dict) -> int | float:
    p = item.get("priority")
    if isinstance(p, bool):
        return math.inf
    if isinstance(p, int):
        # 整数は任意精度で常に有限。math.isfinite は float に変換するので、
        # 10**400 のような値で OverflowError になり、カタログ全体が unknown に落ちる。
        return p
    if isinstance(p, float) and math.isfinite(p):
        return p
    return math.inf


def _fetch_codex(engine_id: str) -> ModelList:
    # 内部形式なので防御的に読む。codex は実行のたびにサーバーから取り直して保存している。
    path = _codex_cache_path()
    source = str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ModelList(
            engine_id, "missing", source=source,
            notes=(("backend.models.codex_cache_missing", {"path": source}),),
        )
    except OSError as e:
        return ModelList(engine_id, "unknown", source=source, detail=f"cannot read: {e}")
    try:
        data = json.loads(text)
    except ValueError:
        return ModelList(engine_id, "unknown", source=source, detail="not valid JSON")
    if not isinstance(data, dict):
        return ModelList(engine_id, "unknown", source=source, detail="top level is not an object")
    raw = data.get("models")
    if not isinstance(raw, list):
        return ModelList(engine_id, "unknown", source=source, detail="models is not a list")
    items = [
        it for it in raw
        if isinstance(it, dict)
        and isinstance(it.get("slug"), str) and it["slug"].strip()
        and ("visibility" not in it or it["visibility"] == "list")
    ]
    models: list[str] = []
    for it in sorted(items, key=_priority):     # sorted は安定: 同じ priority は元の順
        slug = it["slug"].strip()
        if slug not in models:
            models.append(slug)
    fetched_at = data.get("fetched_at")
    return ModelList(
        engine_id, "ok", models=tuple(models), source=source,
        fetched_at=fetched_at if isinstance(fetched_at, str) else "",
    )


def append_new(base, fetched, *, also_present=()) -> tuple[list[str], list[str], list[str]]:
    """``(merged, added, absent)``。追記のみで、``base`` の順序・中身は変えない。

    ``absent`` は ``base`` のうち取得一覧にも ``also_present``（claude のエイリアスの解決先）
    にも無いもの。ログ表示用で、削除には使わない。
    """
    clean: list[str] = []
    for v in fetched:
        if not isinstance(v, str):
            continue
        v = v.strip()
        if v and v not in clean:
            clean.append(v)
    base_list = list(base)
    added = [v for v in clean if v not in base_list]
    merged = base_list + added
    present = set(clean) | set(also_present)
    absent = [v for v in base_list if v not in present]
    return merged, added, absent


def append_fetched(engine: engines.Engine, ml: ModelList) -> AppendOutcome:
    """取得結果の新規分を候補リストへ追記する（GUI はこれだけを呼ぶ）。

    土台は ``engines.update_choices`` が models.toml のロックを持ったまま読み直した、その
    時点のディスク上のリスト（未カスタマイズなら種）。新規が無ければ書かない。保存の失敗は
    例外のまま呼び出し側へ返す。
    """
    if ml.state != "ok":
        return AppendOutcome()
    result = AppendOutcome()

    def _edit(current: tuple[str, ...]) -> list[str] | None:
        nonlocal result
        merged, added, absent = append_new(
            current, ml.models, also_present=[r for _, r in ml.aliases]
        )
        result = AppendOutcome(added=tuple(added), absent=tuple(absent))
        return merged if added else None

    written = engines.update_choices(engine, "model", _edit)
    return AppendOutcome(added=result.added, absent=result.absent, written=written is not None)
