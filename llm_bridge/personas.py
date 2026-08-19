"""チャットのペルソナ定義ストア (data/llm_state/personas.json)。

PC ローカル (gitignored の data/ 配下)・GUI 管理。ファイルが**不在の間だけ**
SEED_PERSONAS を提示し、初回書込で「シード＋編集」を実体化する。以後はファイルが
正で、空リスト ``{"personas": []}`` もシードを復活させない (engines.saved_choices
の None-vs-() と同じ規律 — 削除後復活バグの排除)。

unreadable (破損・version≠1・形式不正) は読み ``[]``・書き拒否 —
update_ui_pref の兄弟キー保護と同じ理由で、空編集が破損ファイルを黙って潰さない。
ただし primary/.bak がどちらも 0 バイトなら守る中身が無いので書き直す。
API はすべて never-raise で bool が成功を表す。キャッシュは持たない
(極小ファイルで backend build 時と dialog でしか読まない)。
``personas_path`` は conftest の monkeypatch を効かせるため関数内 late import。

書込は ``common.paths.durable_write_json`` (primary + ``.bak`` の 2 コピー・単調 ``_seq``・
読取は newest-wins)。CLAUDE.md の「**非再計算の JSON は durable**」規律に該当する —
ユーザーが手で書いた定義で再生成できず、``config_share.PORTABLE_FILES`` にも不参加なので
他にコピーが無い。同じ ``data/llm_state`` でも ui_prefs / recent_datasets /
backend_sessions は再計算可能なので ``atomic_write_text`` の 1 コピーで足りる。

ファイル形式: ``{"version": 1, "personas": [{"name": ..., "text": ...}]}``
(リスト＝表示順保持)。名前は strip 後非空を強制 — ``""`` は「明示的にペルソナ
なし」のセンチネルとして予約されている。
"""
from __future__ import annotations

from dataclasses import dataclass

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Persona:
    name: str
    text: str


# ファイル不在の間だけ提示される既定ペルソナ (初回書込で実体化)。
SEED_PERSONAS = (
    Persona(
        "粗野な知識人",
        "すぐ本題に入る。相槌を打たない。常に批判的。誤りを容赦なく正す。"
        "結論の後に余計な質問をしない。極めて荒々しく乱暴な口調だが、"
        "発言内容は正確で説明は懇切丁寧。",
    ),
    Persona(
        "怠惰な偏執狂",
        "面倒くさがりで、価値を付加しないことを一切言わない。"
        "だが異常、矛盾、説明の綻びを見つけると急に執拗になり、口角泡を飛ばし暴走する。"
        "情報量の低い話は冷淡な態度で雑に切り上げる。"
        "関心のある怪しい点についてだけ異様に詳細に、疑い深く、繰り返し話したがる。",
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
    ``durable_read_json`` は primary と ``.bak`` の**両方**を読んで新しい方を返すので、
    primary の書込だけが失敗しても ``.bak`` から復旧できる ('recovered' も ok 扱い)。
    """
    from common.paths import durable_read_json
    from llm_bridge.paths import personas_path
    try:
        status, data = durable_read_json(personas_path())
    except (OSError, ValueError, TypeError):
        return ("unreadable", [])
    if status == "absent":
        return ("seed", list(SEED_PERSONAS))
    if status not in ("ok", "recovered"):
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


def _has_content(path) -> bool:
    """primary か ``.bak`` のどちらかに中身があるか。両方 0 バイトなら守るものが無い。

    サイズが取れない（不在以外の OSError）ときは判定不能なので守る側に倒す。
    """
    from pathlib import Path
    from common.paths import bak_path
    for p in (Path(path), bak_path(path)):
        try:
            if p.stat().st_size > 0:
                return True
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return False


def save_personas(personas) -> bool:
    """全定義を正規化して durable 書込 (primary + ``.bak``)。

    unreadable なファイルは潰さない (bool=False)。ただし 2 コピーとも 0 バイトなら
    守る中身が無いので書き直す。空リストもそのまま書く — シードは復活しない。never raise。
    """
    from common.paths import durable_write_json
    from llm_bridge.paths import personas_path
    try:
        path = personas_path()
        if _load()[0] == "unreadable" and _has_content(path):
            return False                     # 破損/version 不一致 → 空編集で潰さない
        payload = {
            "version": SCHEMA_VERSION,
            "personas": [{"name": p.name, "text": p.text}
                         for p in _dedupe(personas)],
        }
        durable_write_json(path, payload)
        return True
    except (OSError, ValueError, TypeError):   # MountWriteError は OSError のサブクラス
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
