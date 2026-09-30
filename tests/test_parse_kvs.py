"""CLI の k=v 解析（Issue #100 D-6）: 名前・パス・自由文のキーは文字列のまま、
それ以外は普通の 10 進数のときだけ数値にする。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from llm_bridge.__main__ import _parse_kvs


@pytest.mark.parametrize(
    ("kv", "key", "expected"),
    [
        # 文字列キー
        ("name=123456", "name", "123456"),
        ("dataset=1.5", "dataset", "1.5"),
        ("session=123e4", "session", "123e4"),
        ("text=1e3", "text", "1e3"),
        # 変換されるもの
        ("x=-3", "x", -3),
        ("x=+2.5", "x", 2.5),
        ("x=.5", "x", 0.5),
        ("x=5.", "x", 5.0),
        ("x=1e3", "x", 1000.0),
        ("index=0", "index", 0),
        # 変換されないもの
        ("x=2024_05", "x", "2024_05"),
        ("x=nan", "x", "nan"),
        ("x=inf", "x", "inf"),
        ("x=1e999", "x", "1e999"),
        ("x=１２３", "x", "１２３"),
        ("x= 12", "x", " 12"),
        ("x=12\n", "x", "12\n"),
        ("x=0x10", "x", "0x10"),
        ("x=", "x", ""),
        ("detail=false", "detail", "false"),
    ],
)
def test_parse_kvs(kv, key, expected):
    out = _parse_kvs([kv])
    assert out[key] == expected
    assert type(out[key]) is type(expected)


def test_parse_kvs_requires_equals():
    with pytest.raises(SystemExit):
        _parse_kvs(["k"])


def test_list_tabs_detail_false_string():
    from llm_bridge import _rewire_window

    win = MagicMock()
    win.tab_names.return_value = ["a"]
    win.tabs.return_value = [
        SimpleNamespace(name="a", session_spec={"dataset": "d", "kind": "analysis"})
    ]
    _rewire_window(win)
    fn = next(
        c.args[1] for c in win.register_command.call_args_list if c.args[0] == "list-tabs"
    )
    assert fn(detail="false") == ["a"]
    assert fn(detail="true") == [{"name": "a", "dataset": "d", "kind": "analysis"}]
    with pytest.raises(ValueError):
        fn(detail="bogus")
