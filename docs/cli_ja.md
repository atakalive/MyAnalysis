# CLI リファレンス

[← README に戻る](../README_ja.md)

```bash
python -m llm_bridge <verb> ...
```

- リポジトリ直下で、GUI と同じ venv の Python で実行する（別の場所からなら `PYTHONPATH=<リポジトリ>` を設定）。
- `window` / `tab` は、起動中の GUI にコマンドを渡して実行させる。仕組みは `data/llm_state/commands/` にファイルを置くだけなので、次の点に注意する。
  - **GUI が起動していなくてもエラーにならない**（ID を表示して終了コード 0）。そのコマンドは次回起動時に捨てられる。起動確認には `python -m llm_bridge window list-tabs --wait 3`（終了コード 1 なら未起動か無応答）。
  - `--wait [秒]`（既定 30 秒）は**必ず末尾に置く**。結果の JSON を表示する。タイムアウトすると終了コード 1 だが、GUI が動いていればコマンドは後で実行される（未起動なら次回起動時に捨てられる）。`--wait` を付けないと、結果は `data/llm_state/command_log.jsonl` に記録されるだけ。ただし `meeting-start` / `meeting-token` / `meeting-lan-link` の結果はログでは `<redacted>` になり、本体は `--wait` で受け取るまで `data/llm_state/results/` に残る。
  - **終了コード 0 でも成功とは限らない。** JSON の `status` が `ok` 以外（`error` / `rejected` / `stale` など）なら理由は `error` にある。`ok` でも `result` が `error:…` や `false` なら失敗。
  - `path=` は絶対パスで渡す。`k=v` の値は、`name` / `dataset` / `path` / `slot` / `text` などの名前・パス・自由文のキーでは文字列のまま、それ以外は普通の 10 進数（`12`・`-3`・`0.5`・`1e3`）のときだけ数値に変換される。`true` / `false` は文字列のまま。
- 出力をファイルやパイプに流すときに日本語が化ける場合は、環境変数 `PYTHONUTF8=1` を設定する。

## 主な verb

| verb | 引数 | GUI | 用途 |
|---|---|---|---|
| `list-datasets` | `[--json]` | 不要 | 登録済みデータセットの一覧 |
| `register-dataset` | `<名前> <絶対パス> [--host H] [--format csv_per_subdir\|custom] [--no-open]` | 不要 | データセットの登録（→ [登録](usage_ja.md#登録)） |
| `engines` | — | 不要 | AI エンジンの導入状況 |
| `doctor` | `[--dataset DS] [--repair] [--rescue] [--cache DIR] [--log FILE]` | 不要 | 同期ドライブ上のファイルの点検（→ [doctor](troubleshooting_ja.md#doctor点検と修復)） |
| `recover-analysis` | `<解析名> [--dataset DS]` | 不要※ | 0 バイトになった `analysis.py` を `.bak` から戻す |
| `draft-analysis` / `apply-analysis` | `<解析名> [--dataset DS]` | 不要※ | 解析コードを安全に編集する（→ [同期ドライブ上で安全に編集する](analysis_module_ja.md#同期ドライブ上で安全に編集する)） |
| `annotate` | `<解析名> marker\|note [k=v …] [--dataset DS]` | 不要※ | 解析タブに注釈（marker / note）を追加する（表示は解析モジュールの実装次第） |
| `clear-annotations` | `<解析名> [marker\|note] [--dataset DS]` | 不要※ | 注釈を消す |
| `config-sync` / `config-push` / `config-pull` | `[--dry-run] [--key K]`（sync / push は `[--include-env]` も） | 不要 | R2 設定同期（→ [複数 PC での設定同期](config_sync_ja.md#複数-pc-での設定同期)） |
| `list-analyses` | `[--dataset DS] [--json]` | 不要※ | 開いているデータセットの解析一覧 |
| `active` / `list-open-datasets` | — | 不要※ | アクティブなタブ・データセット |
| `state` | `[解析名] [--dataset DS]` | 不要※ | 解析の表示状態（`current.json`） |
| `window` | `<サブコマンド> [k=v …] [--wait [秒]]` | **必要** | ウィンドウ操作（→ [`window` のサブコマンド](#window-のサブコマンド)） |
| `tab` | `<タブ名> <サブコマンド> [k=v …] [--wait [秒]]` | **必要** | タブ操作（→ [`tab` のサブコマンド](#tab-のサブコマンド)） |

※ GUI が最後に書いた状態ファイル（`active.json`）を使う。GUI を閉じた後は前回の状態が出る。`--dataset` を省略すると、GUI で最後に前面（アクティブ）だったデータセットが対象になるので、書き込む verb では明示する。

## `window` のサブコマンド

| サブコマンド | 引数 | 結果の例 |
|---|---|---|
| `open-dataset` | `name=<ds>` | `restored:N` / `no-session:<ds>` / `unreadable-session:<ds>`（session.json が壊れていてタブを復元できない）/ `error:<ds>` |
| `set-active-dataset`（別名 `switch-dataset`） | `name=<ds>` | `active:<ds>` |
| `close-dataset` | `name=<ds>` | `closed:<ds>:<n>` / `error:<ds>`（保存に失敗） |
| `list-open-datasets` | — | 開いているデータセットとアクティブ |
| `add-tab` | `name=<解析名> [dataset=<ds>]` | `added:` / `already-present:`（先に `open-dataset` する） |
| `close-tab` / `set-active-tab` | `name=<タブ名> [dataset=<ds>]` | `true` / `false` |
| `list-tabs` | `[detail=1]` | タブの一覧 |
| `show` | `path=<絶対パス> [name=viewer] [slot=<パス>] [dataset=<ds>]` | 図ビューアに表示。`slot=right` / `bottom` で 2 枚目を並べる。`top/left` のように `/` で繋ぐと入れ子分割（2×2 など、深さ 6 まで） → `shown:<名前>` / `updated:<名前>` |
| `show-image` | `path=<絶対パス> [name=image] [panel=left\|right] [slot=<パス>] [dataset=<ds>]` | 画像ビューアに表示。`slot=` は `show` と同じ分割パス（図と生画像をペインごとに混在可）。`slot` 省略時は最初の画像ペインをその場で更新 |
| `toggle-chat-float` | — | チャットの切り離し/格納 |
| `meeting-start` / `meeting-token` / `meeting-lan-link` / `meeting-stop` | → [ミーティング共有](meeting_share_ja.md#コマンドから操作する) | |
| `reload` | `scope=tab target=<解析名>` | 解析タブの再読み込み → `reloaded-tab:<名前>` / `reload-tab-error:…` / `reload-busy:…`（モーダルダイアログの表示中は実行されない。チャットの応答中でも実行する） |

## `tab` のサブコマンド

- 全タブ共通: `set-split left=<n> right=<n> [slot=<領域>]`（比率。`top=`/`bottom=` も可。`slot` 省略時は最外側の分割）、`close-pane slot=<パス>`（ペイン/領域を閉じる。最後の 1 枚は閉じられない → `close-tab`）、`list-panes`（現在のペインを `[{slot, key, kind, path}]` で木の順に返す）、`snapshot`（解析タブの表示を `<work_dir>/analyses/<解析名>/state/current_view.png` に保存。図・画像ビューアのタブでは何もしない）。
- **slot は分割パス**: `left|right` はその段を左右に、`top|bottom` は上下に分ける（`left`≡`top` が 1 番目、`right`≡`bottom` が 2 番目）。配置（`show`/`show-image`）はその段の向きを書いた側に変える——向きが変わると他ペインの slot 名も変わる（`top/left` → `left/top` など）ので `list-panes` で確認する。同じ slot に再表示すると、その場で更新。埋まっているペインを通るパスは、そのペインを分割して旧内容を反対側へ寄せる。パネル key（`figure`/`figure-2`/`viewer-2`…）は構築順の識別子で位置を表さない。ペインは必ず slot で指す。
- 画像ビューアのタブ: `set-lut lut=<Grays|Red|Green|Blue|Magenta|Cyan|Yellow|Fire|Ice|Spectrum> [invert=true]`、`set-range min= max=`、`auto-contrast [low=0.35] [high=99.65]`、`set-channel index=`、`set-mode mode=single|composite`、`set-z index=`、`set-t index=`、`set-visible channel= visible=true|false`、`load-image path=`。`set-lut` / `set-range` / `auto-contrast` は `channel=<n>` も取る（省略時は選択中のチャンネル）。番号は 0 から。いずれも `slot=<パス>` で対象ペインを指定できる（省略時は最初の画像ペイン。これらは向きを変えず、実際の分割と合わない slot はエラーになり現在の slot 一覧を返す）。
- 解析タブ: 各 `analysis.py` が登録したコマンド。
- `tab` コマンドは対象のタブを前面に出す。同じ名前のタブが複数のデータセットにあるときは `dataset=<ds>` を付ける。
- `dataset=` に開いていないデータセットを指定して出したタブ（`add-tab` / `show` / `show-image`）は、そのデータセットに保存済みのタブ構成（`session.json`）があると保存されない（上書きを防ぐため）。先に `open-dataset` する。

## 例

```bash
python -m llm_bridge list-datasets --json
python -m llm_bridge register-dataset my_ds "D:/Data/my_ds" --format csv_per_subdir
python -m llm_bridge window open-dataset name=my_ds --wait 120
python -m llm_bridge window add-tab name=my_analysis dataset=my_ds --wait 60
python -m llm_bridge window show path="D:/Data/my_ds/_work/figures/overview.png" dataset=my_ds --wait
python -m llm_bridge window show-image path="D:/Data/my_ds/raw/cell01.tif" name=raw dataset=my_ds --wait
python -m llm_bridge tab raw auto-contrast --wait
python -m llm_bridge tab my_analysis set-split left=2 right=1 dataset=my_ds --wait
# 2×2（順不同。1 枚を show-image の TIFF にしてもよい）
python -m llm_bridge window show path="D:/Data/my_ds/_work/figures/a.png" name=q slot=top/left --wait
python -m llm_bridge window show path="D:/Data/my_ds/_work/figures/b.png" name=q slot=top/right --wait
python -m llm_bridge window show path="D:/Data/my_ds/_work/figures/c.png" name=q slot=bottom/left --wait
python -m llm_bridge window show-image path="D:/Data/my_ds/raw/d.tif" name=q slot=bottom/right --wait
python -m llm_bridge tab q list-panes --wait
```

## その他の CLI

| コマンド | 用途 |
|---|---|
| `python -m newanalysis <解析名> --dataset <ds> [--format …]` | 解析モジュールの雛形を作る |
| `python -m export <ds> <解析名>` | PNG の一括出力 |
