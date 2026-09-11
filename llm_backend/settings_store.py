"""Targeted TOML key rewriting for backend/model settings (Qt-independent).

GUI からバックエンド/モデル選択を適用するとき、真実ソースの TOML
(``llm_backend/config.toml`` / ``models.toml``) の該当キーだけを書き換える。
コメント・整列パディング・無関係行・改行スタイル (CRLF/LF) を保存する。

設計判断: ``tomlkit``/``tomli_w`` 等の構造保持ライブラリは repo に未導入
(依存方針は PySide6/pyqtgraph/numpy のみ)。依存を増やさず、このモジュール自身が
ロック内で TOML の source テキストを行/ブロック単位に編集し、値は ``json.dumps`` で
常に妥当化する。その代わり「触れる形」を単一行の basic string /
bool / 文字列配列に厳格限定し、範囲外は一切書かず ``RuntimeError`` (fail-closed) と
することで silent corruption を構造的に排除する。
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from common.filelock import exclusive_lock
from common.paths import atomic_write_text, repo_root

_HEADER_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def config_toml_path() -> Path:
    return repo_root() / "llm_backend" / "config.toml"


def models_toml_path() -> Path:
    return repo_root() / "models.toml"


def _format_value(value: str | bool | list[str]) -> str:
    """bool → true/false、str → JSON basic string、list[str] → 単一行配列。

    配列は必ず 1 行で生成する (``["a", "b"]``)。``_apply_one`` の行単位パターンが
    書き換えられるのも単一行配列だけなので、生成側と書換側の形を揃えている。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(json.dumps(v, ensure_ascii=False) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def _find_section(lines: list[str], section: str) -> tuple[int, int] | None:
    """section ヘッダ行 index と body 終端 (次ヘッダ or EOF) を返す。無ければ None。"""
    hdr = None
    for i, ln in enumerate(lines):
        m = _HEADER_RE.match(ln)
        if m and m.group(1).strip() == section:
            hdr = i
            break
    if hdr is None:
        return None
    end = len(lines)
    for i in range(hdr + 1, len(lines)):
        if _HEADER_RE.match(lines[i]):
            end = i
            break
    return hdr, end


def set_toml_keys(
    path: Path, changes: dict[str, dict[str, str | bool | list[str]]]
) -> None:
    """``changes`` = ``{section: {key: value}}`` の該当キーだけを ``path`` に書く。

    値は str / bool / list[str] のみ (それ以外 → ``TypeError``)。コメント・整列・改行・
    末尾改行を保存し、無関係行は verbatim。触れる形は単一行の basic string / bool /
    文字列配列に厳格限定し、multiline・裸の値・重複アクティブキーは書き換えず
    ``RuntimeError`` (fail-closed)。
    書き込み前に ``tomllib.loads`` でラウンドトリップ検証し、失敗ならファイル無変更で
    ``RuntimeError``。既存ファイルが構文破損なら ``.bak`` へ退避して最小再生成する。
    """
    # 値型検証はロック取得前 (ファイルに触れる前) に行う。
    for section, kv in changes.items():
        for key, value in kv.items():
            if isinstance(value, list):
                if not all(isinstance(v, str) for v in value):
                    raise TypeError(
                        f"set_toml_keys: value for [{section}].{key} is a list but "
                        f"contains a non-str element"
                    )
            elif not isinstance(value, (str, bool)):
                raise TypeError(
                    f"set_toml_keys: value for [{section}].{key} must be str, bool "
                    f"or list[str], got {type(value).__name__}"
                )

    path = Path(path)
    lock = path.with_name(path.name + ".lock")
    with exclusive_lock(lock):
        try:
            # newline="" → no newline translation, so CRLF/LF is detectable below.
            with open(path, encoding="utf-8", newline="") as f:
                text: str | None = f.read()
        except FileNotFoundError:
            text = None
        if text is not None:
            try:
                tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                # ユーザーの手編集で全体が壊れている → 退避して最小再生成する
                # (壊れた TOML が GUI からの正常な適用を恒久ブロックしないため)。
                bak = path.with_suffix(path.suffix + ".bak")
                atomic_write_text(bak, text, newline="")
                text = None

        if text is None:
            newline = "\n"
            trailing = True
            lines: list[str] = []
        else:
            newline = "\r\n" if "\r\n" in text else "\n"
            trailing = text.endswith("\n")
            lines = text.splitlines()

        for section, kv in changes.items():
            for key, value in kv.items():
                _apply_one(lines, section, key, _format_value(value), path)

        new_text = newline.join(lines)
        if lines and trailing:
            new_text += newline

        # 安全網: 要求した全キーがラウンドトリップ一致することを検証。壊すくらいなら拒否。
        try:
            parsed = tomllib.loads(new_text)
        except tomllib.TOMLDecodeError as e:  # pragma: no cover - 生成ミスの保険
            raise RuntimeError(
                f"set_toml_keys: generated TOML for {path} is invalid: {e}"
            ) from e
        for section, kv in changes.items():
            sec = parsed.get(section)
            for key, value in kv.items():
                if not isinstance(sec, dict) or sec.get(key) != value:
                    raise RuntimeError(
                        f"set_toml_keys: round-trip check failed for "
                        f"[{section}].{key} in {path} (file left unchanged)"
                    )

        atomic_write_text(path, new_text, newline="")


def _apply_one(
    lines: list[str], section: str, key: str, new_val: str, path: Path
) -> None:
    """1 つの (section, key) を lines に適用 (in-place)。"""
    # val は「単一行の basic string / bool / 文字列配列」のみ。配列は要素に [ ] を
    # 含まない単一行のものだけが一致する (multiline 配列は閉じ括弧が同じ行に無いので
    # 一致せず、下の RuntimeError で fail-closed になる)。
    key_re = re.compile(
        r"^(?P<indent>\s*)" + re.escape(key)
        + r'(?P<eq>\s*=\s*)(?P<val>"(?:[^"\\]|\\.)*"|true|false|\[[^\[\]]*\])'
        + r"(?P<pad>\s*)(?P<cmt>#.*)?$"
    )
    assign_re = re.compile(r"^\s*" + re.escape(key) + r"\s*=")

    loc = _find_section(lines, section)
    if loc is None:
        # セクション自体が無い → EOF に [section] + key を追記 (ファイル無しからの最小生成も同経路)。
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append(f"[{section}]")
        lines.append(f"{key} = {new_val}")
        return

    hdr, end = loc
    active = [
        i
        for i in range(hdr + 1, end)
        if not lines[i].lstrip().startswith("#") and assign_re.match(lines[i])
    ]
    if len(active) > 1:
        raise RuntimeError(
            f"set_toml_keys: duplicate active key {key!r} in [{section}] of "
            f"{path} (ambiguous; file left unchanged)"
        )
    if len(active) == 1:
        i = active[0]
        m = key_re.match(lines[i])
        if m is None:
            raise RuntimeError(
                f"set_toml_keys: key {key!r} in [{section}] of {path} is not a "
                f"single-line string/bool/array (multiline/bare value); refusing "
                f"to rewrite (file left unchanged)"
            )
        lines[i] = (
            f"{m['indent']}{key}{m['eq']}{new_val}{m['pad']}{m['cmt'] or ''}"
        )
        return

    # アクティブ行なし → セクション末尾 (次ヘッダ or EOF の手前、末尾連続空行の手前) に挿入。
    j = end
    while j > hdr + 1 and lines[j - 1].strip() == "":
        j -= 1
    lines.insert(j, f"{key} = {new_val}")
