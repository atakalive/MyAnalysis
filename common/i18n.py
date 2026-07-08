"""軽量 i18n: TOML カタログ + tr()。Qt 非依存・never-raise。"""
from __future__ import annotations

import tomllib

from common.paths import i18n_dir  # 一方向 import（paths は i18n を import しない）

_DEFAULT_LANG = "en"          # 起動既定 = フォールバック基底
_active: str = _DEFAULT_LANG
_catalogs: dict[str, dict[str, str]] = {}


def _load_catalogs() -> None:
    """i18n/*.toml を全て読み直して _catalogs を作り直す。
    top-level の str->str ペアのみ採用し、table/非 str 値はキー単位で破棄する
    （never-raise 保険: 不正な値で tr() が str 以外を返したり .format で落ちない）。
    読込失敗ファイルは {} 扱いで skip。"""
    global _catalogs
    cats: dict[str, dict[str, str]] = {}
    d = i18n_dir()
    if d.is_dir():
        for p in d.glob("*.toml"):
            try:
                with open(p, "rb") as f:
                    raw = tomllib.load(f)
            except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
                cats[p.stem] = {}
                continue
            cats[p.stem] = {
                k: v for k, v in raw.items() if isinstance(v, str)
            }
    _catalogs = cats


def _ensure_loaded() -> None:
    if not _catalogs:
        _load_catalogs()


def tr(key: str, /, **kwargs) -> str:
    """アクティブ言語で訳出。フォールバック: active → en → key。never raise。
    補間失敗（kwargs 不足・型不一致・属性アクセス失敗等）時は未補間テンプレを返す。"""
    _ensure_loaded()
    template = _catalogs.get(_active, {}).get(key)
    if not isinstance(template, str):          # 非 str / 欠落 → 基底へ（空文字 "" は採用）
        template = _catalogs.get(_DEFAULT_LANG, {}).get(key)
    if not isinstance(template, str):
        template = key
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except Exception:                          # never-raise: あらゆる format 例外を吸収
        return template


def available_languages() -> list[str]:
    """i18n/*.toml の stem を昇順で返す（動的検出）。"""
    d = i18n_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.toml"))


def current_language() -> str:
    return _active


def language_display_name(code: str) -> str:
    """言語メニュー用の autonym（自言語表示名）。無ければ code をそのまま。"""
    _ensure_loaded()
    return _catalogs.get(code, {}).get("_lang.name", code)


def set_language(lang: str) -> None:
    """アクティブ言語を切替え、ui_prefs.json に永続化（best-effort）。never raise。"""
    global _active
    _ensure_loaded()
    if lang != _DEFAULT_LANG and lang not in _catalogs:
        return  # 未知言語は無視（安全側）。en は基底なので常に許可
    _active = lang
    from llm_bridge.paths import update_ui_pref  # 遅延 import（循環回避。下記参照）
    update_ui_pref("language", lang)


def init_language() -> None:
    """ウィンドウ構築ごと（通常起動＋Tier 3 app 再構築）に呼ばれ、カタログを読み直し
    ui_prefs の "language" で _active を再ハイドレートする。未設定/不正/未知言語なら en。
    読むだけで永続化しない（冪等）。Tier 3 は common.i18n をパージ→新規 import して
    _active を en 既定に戻すため、構築点でここを通さないと言語が英語に戻る。"""
    global _active
    _load_catalogs()
    from llm_bridge.paths import read_ui_pref  # 遅延 import
    pref = read_ui_pref("language", None)
    _active = pref if isinstance(pref, str) and pref in _catalogs else _DEFAULT_LANG
