# 引継ぎ: 全体テスト（実 MathJax 込み）の時短（作業中）

> 作業中の記録。作業が終わったら、このファイルは最後のコミットで消す。

## 1. 状態（2026-11-01 時点）

| 段階 | 状態 |
|---|---|
| 原因の調査 | **済み**（§2。計測で確定） |
| 計画 | **済み**（§3。レビューの指摘を反映した版） |
| 変更前の計測（計測用プラグイン無しで 3 回） | **未着手** |
| 変更（§3） | **未着手**（テストと本体のコードはまだ何も変えていない） |
| 変更後の計測・コミット・報告 | 未着手 |

**保留した理由**: 同じ PC で別のプロジェクトのテスト（処理時間の計測を含む）が走っていた。全体テストを重ねるとその計測に影響が出るので、利用者の指示で保留した。再開する前に、ほかのプロジェクトの pytest が走っていないことを確かめる（§4 の手順 0）。

## 2. 分かったこと

### 2-1. 遅い原因

全体テスト（約 200 秒）のうち、約 68 秒は `common/paths.py` の書込検証の待ちだった。書込が検証を通らないと、`_VERIFY_BACKOFF = (0.0, 0.15, 0.5, 1.5, 4.0)`（計 6.15 秒）を全部待ってから `MountWriteError` を送出する。

待ちを全部使ったのは 11 回で、その内訳は次の 2 つ。

- **意図しない 8 回（約 49 秒）**: `tests/test_workspace_gui.py` の 3 テスト。原因は `llm_bridge/session.py` の `_touched` が、テストごとに隔離されていないこと。
  - `_touched` はモジュールに 1 つだけある set で、`save_all` が書く先のデータセットの集合になる。
  - `tests/conftest.py` の `_isolate_session_ownership` は、`_restored` / `_unreadable` / `_chat_baseline` / `_last_skipped` は差し替えているが、`_touched` は差し替えていない。
  - そのため、前のテストが `note_dataset` で残した名前を、後のテストの `save_all` が拾う。
  - `win` fixture は `config.get_dataset_dir` を `tmp_path / ds` に向けるので、拾った名前は存在しないディレクトリを指す。その結果は次のとおり。
    1. `ensure_config` が `myanalysis.toml` を書けない（`[Errno 2]`）。
    2. 5 回試して待ちを全部使い、`MountWriteError` になる。
    3. `save_all` がこれを `failed` として飲み込むので、テストは通る。
    4. 失敗した名前は `_touched` に残り続ける。
- **意図した 3 回（約 18 秒）**: どれも、書込を失敗させること自体を確かめるテスト。
  - `test_paths.py::test_atomic_write_{text,bytes}_replace_failure_keeps_old_and_cleans_tmp`
  - `test_recent_datasets.py::test_note_never_raises`

### 2-2. 証拠

計測用プラグイン（§5 の `failtrace2.py`）を入れて全体を 1 回流した。結果は 203.29 秒で、プラグイン付きなので時間の比較には使わない。

| テスト | 開始時の `_touched` | テストが作るデータセット | 書込に失敗したデータセット |
|---|---|---|---|
| test_close_dataset_flush_survives_save_all | ds, dsA, dsB, myds | dsA, dsB | myds, ds（2 回） |
| test_save_session_overwrite_unreadable_yes | ds, dsC, myds | dsU | dsC, myds, ds（3 回） |
| test_save_session_overwrite_unreadable_no | ds, dsC, dsU, myds | dsU | dsC, myds, ds（3 回） |

- どの失敗も `strategy=replace detail=write failed: [Errno 2] No such file or directory: '…\<DS>\myanalysis.toml.<乱数>.tmp'`。
- 名前を残していたテストは次のとおり。
  - `test_attach_tab_dataset`（dsA, dsB）
  - `test_floating_tab`（ds, D）
  - `test_hotreload_qt`（_hr_probe_ds, _hr_probe_ds2）
  - `test_show`（myds, ds）
  - `test_workspace_gui` 自身（dsC, dsU など）
- 時間がかかったテスト（`--durations`）:

  | テスト | 時間 |
  |---|---|
  | unreadable_yes | 18.84 秒 |
  | unreadable_no | 18.71 秒 |
  | flush_survives | 12.40 秒 |
  | test_paths ×2 | 各 6.16 秒 |
  | test_note_never_raises | 6.15 秒 |

- `test_workspace_gui.py` だけを流すと、この 3 テストは各 0.06 秒で終わる。
- 書込の検証に失敗したテストは、ほかに `test_paths.py` の `test_write_that_silently_zeroes_raises_and_reports` と `test_partial_write_is_detected` がある。この 2 つは既に `_no_sleep` で待っていないので、時間はかからない。

### 2-3. ほかに分かったこと

- **6 件の失敗は環境が原因**: `test_math_engine.py` は実 MathJax を 2 つのフォントで確かめるが、`MYANALYSIS_MATHJAX_TEST_ROOT` の導入先に `mathjax-newcm` が入っていない。テストは「導入先を指定したのに起動に失敗したら、テストを失敗にする」方針なので、これは意図どおりで、コードは直さない。
  - 6 件は `test_real_disable_enable_restarts_rendering`、`test_real_stop_order_and_cleanup`、newcm の分の `test_real_svg_xml_preserves_tex` / `test_real_bold_assumption_holds` / `test_real_svg_renders_to_pixels` ×2。
  - 設定 → 数式の描画… から newcm を導入すれば、緑になるはず。
- **`PYTHONIOENCODING=utf-8` が入った環境では `test_register_dataset.py::test_initial_import_fails_fast_on_corrupt_registry` が失敗する**。
  - エージェントのシェルにはこの変数が入っていることがある。普通の端末には無い。
  - 子プロセスがこの変数を受け継いで stderr を UTF-8 で書き、`_run_child` の `subprocess.run(..., text=True)` が cp932 で読もうとして失敗する。
  - 変数を外すと 30 件すべて通る。計測では変数を外す（§5 の `run_suite.ps1` が外す）。
  - テストの側の弱さ（`encoding` を指定していない）は、今回の対象外。
- **これまでの計測の揺れ（190〜285 秒）**: 計測用プラグインの有無が混ざっていたことと、同じ PC でほかのテストが同時に走っていたことの影響を受けている。自然な揺れとは扱わない。
- 実 MathJax の子プロセスは 4 本で計約 4 秒で、主因ではない。

## 3. 計画（レビューの指摘を反映した版）

### 変更

1. **`tests/conftest.py`**: `_isolate_session_ownership` で `_touched` も空の set に差し替える（正しさの修正。約 49 秒）。
   ```python
       from llm_bridge import session
       for name in ("_restored", "_unreadable", "_chat_baseline", "_last_skipped"):
           monkeypatch.setattr(session, name, {})
       monkeypatch.setattr(session, "_touched", set())
   ```
   - docstring に `_touched` と、それが漏れたときの症状（後のテストの `save_all` が、存在しない work_dir に書こうとする）を足す。
   - 各テストに既にある `session._touched.clear()` や `monkeypatch.setattr(session, "_touched", set())` は残す。どちらも同じ関数スコープの `monkeypatch` を使い、teardown では逆の順に戻す。`.clear()` が空にするのは、そのテスト用の set だけ。
   - `_touched` を参照するのは `session.py` だけで、どれも呼ばれたときにモジュールの名前を引く。
2. **意図して失敗させる 3 テストの中だけで、待ちを省く**（約 18 秒）。conftest で一律に 0 にはしない（理由は「やらないこと」）。
   - `tests/test_paths.py` の `test_atomic_write_{text,bytes}_replace_failure_keeps_old_and_cleans_tmp`: 先頭で、同じファイルにある `_no_sleep(monkeypatch)` を呼ぶ。どちらもスレッドを使わないテスト。
   - `tests/test_recent_datasets.py::test_note_never_raises`: `monkeypatch.setattr(common.paths, "_VERIFY_BACKOFF", (0.0,) * len(common.paths._VERIFY_BACKOFF))`（試行の回数は 5 のまま）。
3. **`docs/development_ja.md` §8 の「`tests/conftest.py` が隔離するもの」の表**: 「session の持ち主・チャット baseline の記録」の行に `_touched`（空の set）を足す。

前のテストの残りに頼っていたテストが、変更 1 で落ちたら、そのテストの側で必要な状態を明示的に用意して直す。

### やらないこと

| 案 | 理由 |
|---|---|
| conftest で `_VERIFY_BACKOFF` を一律 0 にする | 待ちに頼る並行のテストが不安定になりうる。意図しない書込の失敗（今回の `_touched` のような）が、durations に出なくなる |
| 読取リトライの待ち（`read_json_classified` の `delay=0.05`、160 回で約 8 秒）を定数に出して 0 にする | 本番の引数の形を変える割に効果が小さい。8 つほどのモジュールに散らばっている |
| `test_math_engine` の作業を減らす（2〜3 秒） | 実 MathJax で確かめる範囲が狭くなる |
| `test_git` の timeout を縮める、xdist で並列にする、0.3 秒未満の sleep を削る | 不安定になる・依存が増える・効果が小さい |

### コミット（main に直接。push は origin だけ）

1. `test: conftest で session._touched をテストごとに隔離する`（変更 1・3）
2. `test: 意図して書込を失敗させるテストで検証の待ちを省く`（変更 2）
3. 最後に、このファイルを消す（どちらかのコミットに含めてもよい）

## 4. 再開の手順

PowerShell で、リポジトリ直下から実行する。全体テストは 1 回が約 3.5 分かかる。

- **1 回ごとに、別々のバックグラウンドのコマンドで流す**（`run_suite.ps1 -N 1`）。
  - `-N 3` を 1 つのコマンドにすると、ツールのタイムアウト（600 秒）を超える。
  - `Start-Process` で切り離して起動したプロセスは、エージェントのセッションが終わると一緒に止まった。
- **計測の途中でテストのファイルを編集しない**。後の回が、新しいコードを読み込んでしまう。

0. **ほかのプロジェクトの pytest が走っていないことを確かめる**。走っていれば流さず、利用者に伝える。
   ```powershell
   Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'pytest' } | Select-Object ProcessId, CommandLine
   ```
1. **変更前の計測**: `run_suite.ps1 -Tag before -N 1` を 3 回。
   - 各回の時間と、失敗・skip の nodeid と理由を残す（`-rfEs` の出力）。
   - 参考: 前回、計測用プラグイン無しで流したときは 200.59 秒（3128 passed / 6 failed / 6 skipped）。その回は `--durations=0` を付けていて、今回のコマンドとは違うので、中央値には入れない。
2. 変更 1〜3 を入れて、関係するテストを流す（数十秒。手順 0 と同じ確認をしてから）。
   ```powershell
   python -m pytest tests/test_paths.py tests/test_recent_datasets.py tests/test_workspace_gui.py tests/test_session.py tests/test_show.py tests/test_show_image.py -q
   ```
3. **変更後に、原因が消えたことを確かめる**: `run_suite.ps1 -Tag after-trace -N 1 -Trace`。
   - `data/timing/failures.log` の `FAIL` が、次の 5 行だけになっていること。
     - test_paths の 4 件（replace_failure ×2・silently_zeroes・partial_write）
     - test_recent_datasets の 1 件
   - workspace_gui の `FAIL` が消えていること。この 3 テストには待ちを省く変更を入れていないので、消えたのは変更 1 の効果だと言える。
4. **変更後の計測**: `run_suite.ps1 -Tag after -N 1` を 3 回。
   - 失敗・skip の nodeid と理由の集合が、変更前と同じであること。
   - workspace_gui の 3 テストが、各 0.1 秒前後になっていること。
5. §3 のとおりにコミットし、push する。
6. **報告**: 次の内容を伝える。
   - 変更前と変更後、それぞれ 3 回の時間と中央値
   - 原因を確かめた結果
   - 失敗・skip の集合が一致したか
   - 限界: newcm の経路はこの環境では確かめていないこと、WSL では計測していないこと

**見込み**: 変更前の中央値から約 67 秒短くなる（約 200 秒 → 約 133 秒）。

## 5. 計測用の道具

`data/` は Git の管理外なので、ここに中身を残す。このファイルを書いた PC では、`data/timing/` に同じものがある。

### `data/timing/run_suite.ps1`

```powershell
param(
    [Parameter(Mandatory = $true)][string]$Tag,
    [int]$N = 1,
    [switch]$Trace
)
# 全体テスト（実 MathJax 込み）を同じコマンドで N 回流し、各回の出力を data/timing/<Tag>-<i>.log に残す。
# リポジトリ直下で実行する。
$ErrorActionPreference = 'Stop'
$env:MYANALYSIS_MATHJAX_TEST_ROOT = Join-Path $HOME '.myanalysis/mathjax'
$env:PYTHONPYCACHEPREFIX = Join-Path $env:TEMP 'myanalysis-pyc'
# エージェントのシェルが入れる PYTHONIOENCODING は普通の端末に無い（子プロセスの出力の読み方が変わる）
Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue
$argv = @('-m', 'pytest', 'tests/', '-q', '-rfEs', '--durations=30')
if ($Trace) {
    $env:PYTHONPATH = 'data/timing'
    $argv += @('-p', 'failtrace2')
}
# 同じ PC で走っている他の pytest（自分以外）と CPU 負荷。比較の条件の記録用。
function Get-Load {
    $others = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match 'pytest' -and $_.CommandLine -notmatch ' tests/ ' }).Count
    $cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
    "others=$others cpu=$cpu%"
}
for ($i = 1; $i -le $N; $i++) {
    $out = "data/timing/$Tag-$i.log"
    $err = "data/timing/$Tag-$i.err"
    $load0 = Get-Load
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $p = Start-Process -FilePath 'python' -ArgumentList $argv -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $out -RedirectStandardError $err
    $sw.Stop()
    $load1 = Get-Load
    $summary = Get-Content $out -Tail 1
    "$Tag-$i rc=$($p.ExitCode) wall=$([math]::Round($sw.Elapsed.TotalSeconds, 2))s [start $load0 / end $load1] :: $summary" |
        Out-File -Append -Encoding utf8 'data/timing/runs.txt'
}
```

- `-N 1` で回すときは、`-Tag` を変えるか、ログが上書きされないように連番の `-Tag`（`before1` / `before2` …）にする。
- 実行は `powershell -NoProfile -ExecutionPolicy Bypass -File data/timing/run_suite.ps1 -Tag before1`（バックグラウンドで）。

### `data/timing/failtrace2.py`（`-Trace` のときに読み込む計測用プラグイン）

```python
"""計測用プラグイン: MountWriteError と session._touched の残りを記録する（時間の比較には使わない）。

- FAIL: atomic_write_* が検証に失敗した（_write_verified が MountWriteError を送出した）書込
- SETUP: test_workspace_gui のテストが始まる時点（fixture の setup より前）の session._touched
- TOUCHED: テストの teardown 後に session._touched が変わったとき（どのテストが残したか）
"""
import sys

import pytest

import common.paths as cp

LOG = "data/timing/failures.log"
_cur = {"nodeid": None}
_last = {"t": None}
_real = cp._write_verified


def _w(line):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def traced(path, data, **kw):
    try:
        return _real(path, data, **kw)
    except cp.MountWriteError as e:
        _w(f"FAIL [{_cur['nodeid']}] path={path} strategy={cp._strategy_for(path)} detail={e.detail}")
        raise


cp._write_verified = traced


def _touched():
    s = sys.modules.get("llm_bridge.session")
    return sorted(s._touched) if s is not None else None


def pytest_configure(config):
    open(LOG, "w", encoding="utf-8").close()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    _cur["nodeid"] = item.nodeid
    if "test_workspace_gui.py" in item.nodeid:
        _w(f"SETUP [{item.nodeid}] _touched={_touched()}")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    yield
    t = _touched()
    if t != _last["t"]:
        _w(f"TOUCHED after [{item.nodeid}] -> {t}")
        _last["t"] = t
```

- 変更 1 を入れた後は、conftest がテストごとに `_touched` を差し替えて teardown で元の set に戻す。そのため、`TOUCHED` の行はほとんど出なくなる（出たら、どこかが元の set に直接書いている）。
