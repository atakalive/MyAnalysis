"""チャットのペルソナ定義ストア (data/llm_state/personas.json)。

PC ローカル (gitignored の data/ 配下)・GUI 管理。ファイルが**不在の間だけ**
SEED_PERSONAS を提示し、初回書込で「シード＋編集」を実体化する。以後はファイルが
正で、空リスト ``{"personas": []}`` もシードを復活させない (engines.saved_choices
の None-vs-() と同じ規律 — 削除後復活バグの排除)。

unreadable (破損・version≠1・形式不正) は読み ``[]``・書き拒否 —
update_ui_pref の兄弟キー保護と同じ理由で、空編集が破損ファイルを黙って潰さない。
API はすべて never-raise で bool が成功を表す。キャッシュは持たない
(極小ファイルで backend build 時と dialog でしか読まない)。
``personas_path`` は conftest の monkeypatch を効かせるため関数内 late import。

ファイル形式: ``{"version": 1, "personas": [{"name": ..., "text": ...}]}``
(リスト＝表示順保持)。名前は strip 後非空を強制 — ``""`` は「明示的にペルソナ
なし」のセンチネルとして予約されている。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Persona:
    name: str
    text: str


# ファイル不在の間だけ提示される既定ペルソナ (初回書込で実体化)。
SEED_PERSONAS = (
    Persona(
        "インテリDQN",
        "すぐ本題に入る。相槌を打たない。常に批判的。誤りを容赦なく正す。"
        "結論の後に余計な質問をしない。極めて荒々しく乱暴な口調だが、"
        "発言内容は正確で説明は懇切丁寧。",
    ),
)


def _dedupe(personas) -> list[Persona]:
    """名前 strip・空白名スキップ・重複先勝ち。text は str 以外を "" に落とす。"""
    out: list[Persona] = []
    seen: set[str] = set()
    for p in personas:
        name = p.name.strip() if isinstance(p.name, str) else ""
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(Persona(name, p.text if isinstance(p.text, str) else ""))
    return out


def _load() -> tuple[str, list[Persona]]:
    """3 状態で読む: ("seed", シード) / ("ok", ファイル内容) / ("unreadable", []).

    absent はシード提示 (書込可)、unreadable は読み []・書き拒否。
    """
    from common.paths import read_json_classified
    from llm_bridge.paths import personas_path
    try:
        status, data = read_json_classified(personas_path())
    except (OSError, ValueError, TypeError):
        return ("unreadable", [])
    if status == "absent":
        return ("seed", list(SEED_PERSONAS))
    if status != "ok":
        return ("unreadable", [])
    version = data.get("version")
    # bool は int のサブクラス (True == 1) なので明示的に弾く。
    if version != SCHEMA_VERSION or isinstance(version, bool):
        return ("unreadable", [])
    raw = data.get("personas")
    if not isinstance(raw, list):
        return ("unreadable", [])
    return ("ok", _dedupe(
        Persona(rec.get("name"), rec.get("text"))
        for rec in raw if isinstance(rec, dict)
    ))


def list_personas() -> list[Persona]:
    """表示順の全ペルソナ。ファイル不在ならシード、unreadable なら []。never raise。"""
    return _load()[1]


def get_persona(name: str) -> Persona | None:
    """名前で 1 件引く。無ければ None。never raise。"""
    want = name.strip() if isinstance(name, str) else ""
    if not want:
        return None                          # "" は「明示的になし」センチネル
    for p in list_personas():
        if p.name == want:
            return p
    return None


def save_personas(personas) -> bool:
    """全定義を正規化して書込む (tmp+replace、update_ui_pref と同機構)。

    unreadable なファイルは潰さない (bool=False)。空リストもそのまま書く —
    シードは復活しない。never raise。
    """
    from llm_bridge.paths import personas_path
    try:
        if _load()[0] == "unreadable":
            return False                     # 破損/version 不一致 → 空編集で潰さない
        payload = {
            "version": SCHEMA_VERSION,
            "personas": [{"name": p.name, "text": p.text}
                         for p in _dedupe(personas)],
        }
        path = personas_path()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)
        return True
    except (OSError, ValueError, TypeError):
        return False


def upsert_persona(name: str, text: str) -> bool:
    """1 件追加/更新。初回書込はシードごと実体化する。bool = 成功。never raise。"""
    want = name.strip() if isinstance(name, str) else ""
    if not want:
        return False                         # 空白名は拒否 ("" はなしセンチネル)
    status, personas = _load()
    if status == "unreadable":
        return False
    body = text if isinstance(text, str) else ""
    out = [Persona(want, body) if p.name == want else p for p in personas]
    if all(p.name != want for p in personas):
        out.append(Persona(want, body))
    return save_personas(out)


def delete_persona(name: str) -> bool:
    """1 件削除。シード由来でも削除は永続する (再読込で復活しない)。

    対象が無ければ書込せず成功 (冪等 — 不在ファイルを実体化する副作用も出さない)。
    never raise。
    """
    want = name.strip() if isinstance(name, str) else ""
    if not want:
        return False
    status, personas = _load()
    if status == "unreadable":
        return False
    out = [p for p in personas if p.name != want]
    if len(out) == len(personas):
        return True                          # 変化なし → 書かない
    return save_personas(out)
