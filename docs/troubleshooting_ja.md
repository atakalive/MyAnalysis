# 同期ドライブとトラブルシューティング

[← README に戻る](../README_ja.md)

## 同期ドライブでの書き込み

rclone などのマウントでは、「一時ファイルに書いて置き換える」方式の保存が、エラーも出さずにファイルを 0 バイトにしてしまうことがある。MyAnalysis は保存先のファイルシステムを自動で判定し、壊れやすいもの（fragile）では直接上書きしてから読み戻して確認する（最大 5 回・約 6 秒まで試し、それでも駄目なら保存失敗として扱う）。失敗するとステータスバーに「保存に失敗しました（検証NG）…」と出る。

| 保存先 | 判定 |
|---|---|
| rclone をドライブ文字にマウント（WinFsp） | fragile |
| ネットワークドライブ（SMB / NAS）、UNC パス（`\\server\share`） | fragile |
| Linux の FUSE（rclone / sshfs 等）・CIFS・NFS | fragile |
| OneDrive / Dropbox / Google ドライブのミラー（C: などの下のフォルダ） | local |
| Google ドライブのストリーミング（仮想ドライブ文字） | ドライブが報告するファイルシステム名による（doctor で確認する） |
| **rclone をフォルダにマウント**（例 `C:\mnt\sync`） | **local と誤判定される** → ドライブ文字でマウントするか、下の `MYANALYSIS_FS_OVERRIDE` で指定する |
| macOS（すべて） | 自動では local。必要なら `MYANALYSIS_FORCE_FRAGILE=1` |

- 判定は `python -m llm_bridge doctor --dataset <ds>` の最初の節で確認できる。判定はドライブごとにアプリ起動中は記憶されるので、マウントを変えたらアプリを再起動する。
- 読み戻し確認はキャッシュを読むので、「クラウドへのアップロード完了」を意味しない。PC を終了する前に同期の完了を待つ。
- 同じデータセットを 2 台で同時に開かない。
- rclone 上で `myanalysis.toml` や `analysis.py` をエディタで編集した後は、開けるか確認する（`analysis.py` は [draft → apply](analysis_module_ja.md#同期ドライブ上で安全に編集する) の手順を推奨）。

**判定と書き込み方式の上書き**（OS の環境変数として設定する。`.env` に書いた `MYANALYSIS_*` は GUI にしか効かず、ターミナルから実行する CLI には効かない）:

| 環境変数 | 意味 |
|---|---|
| `MYANALYSIS_FS_OVERRIDE` | 判定の上書き。例 `M:=fragile,C:\mnt\sync=fragile`（前方一致） |
| `MYANALYSIS_WRITE_STRATEGY` | `auto`（既定）/ `inplace` / `replace`（旧方式。rclone では危険） |
| `MYANALYSIS_FORCE_FRAGILE` | `1` で全パスを fragile として扱う |
| `PYTHONPYCACHEPREFIX` | Python のキャッシュをローカルに置く。**`.env` では効かない**ので、起動前に OS / シェルで設定する（`run.bat` は自動で設定する）。`python tool.py` や `python -m export` を手で実行するときは自分で設定するか、`PYTHONDONTWRITEBYTECODE=1`（または `python -B`） |

## doctor（点検と修復）

```bash
python -m llm_bridge doctor [--dataset <ds>] [--repair] [--rescue] [--cache <rcloneのキャッシュ>] [--log <rcloneのログ>]
```

- 出力はファイルやパイプへ流してよい（cp932 で表せない文字は `\uXXXX` で出る）。`--dataset` で対象を絞ると速い。
- 出力は 4 節: ファイルシステムの判定 / データセットごとの点検（0 バイトのファイル、壊れた `myanalysis.toml`、本体と `.bak` の食い違い）/ PC ローカルの状態ファイル / rclone のキャッシュとログ。`!` は要対応（1 つでもあれば終了コード 1）、`-` は情報。空が正常なファイル（`__init__.py`、`py.typed`、`.gitkeep`、`*.lock`）は報告しない（そのため、中身のあった `__init__.py` が 0 バイトになっても報告されず、`--rescue` の対象にもならない）。
- `--dataset` の名前が登録簿に無いとき・この PC のパスが無いときは、何も点検せずにエラーを出して終了コード 2。フォルダが見えないデータセットは `!` で報告する。
- `--repair`: 本体と `.bak` の食い違いを新しい方に揃える。PC ローカルの 0 バイトの状態ファイルを削除する（自動で作り直される）。他の PC を閉じ、同期が落ち着いてから実行する。
- `--rescue`: 0 バイトになったファイルを、rclone のキャッシュに残った一時ファイルから復元する（0 バイトのファイルだけが対象）。**rclone のキャッシュの指定が必要**（`--cache` か環境変数 `MYANALYSIS_RCLONE_CACHE`。無い・見つからないときはエラーで終了コード 2）。`--rescue` を付けずに実行すると復元候補の一覧だけが出るので、先に確認する。
- `--cache` には `<キャッシュ先>/vfs/<リモート名>` を渡す。キャッシュ先は mount の `--cache-dir`、指定していなければ `rclone config paths` の Cache dir。`vfsMeta` を含む上位のフォルダを渡すと、rclone のメタデータを誤って復元するので渡さない。
- rclone の節は、キャッシュ（`--cache` か環境変数 `MYANALYSIS_RCLONE_CACHE`）とログ（`--log`（mount の `--log-file` に指定したファイル）か `MYANALYSIS_RCLONE_LOG`）のうち、それぞれ指定したものだけを点検する。環境変数は OS の環境変数として設定する（`.env` からは読まない）。指定しなかったものは「未指定のためスキップ」と出る。指定したパスが見つからなければ `!` で報告する（終了コード 1）。rclone を使っていなければ、この節は無視してよい。
- 実行後はもう一度 doctor で確認する。

## トラブルシューティング

| 症状 | 主な原因 | 対処 |
|---|---|---|
| `run.bat` でウィンドウが出ない | 依存不足・Python が古い・登録簿の破損 | venv を有効にして `python tool.py` で起動し、エラーを見る |
| 設定を変えたらアプリが起動しない | `[backend].name` / `LLM_BACKEND` の値が不正 | 正しい値（`claude` / `codex` / `pi` / `openai` / `mock`）に直す |
| GUI も全 CLI も `RegistryError` で落ちる | `datasets.local.json` の破損 | ファイルを `datasets.local.json.corrupt` などに退避して手で直す（R2 同期を使っていれば退避後に `config-pull`） |
| 最初の送信が HTTP 401 / 接続拒否 | エンジン未設定、または `llm_backend/config.toml` の構文が壊れている（OpenAI API か `.env` の接続先に送っている） | [エンジンを設定](../README_ja.md#ai-エンジン) |
| `[エラー: Claude Code engine not found…]` | Claude Code が見つからない | 拡張を入れる、`claude` を PATH に通す、または `[claude_code].bin` / `CLAUDE_CODE_BIN` を指定 |
| `[エラー: … not found in PATH…]`（pi / codex） | CLI 未導入 | `npm i -g …`（バックエンドの状況ウィンドウからも可）。Codex は `codex login` も必要 |
| チャットに `[エラー: …]` | エンジン側のエラー（認証切れ・レート制限等） | 多くはそのまま再送で回復。認証切れはログインし直す |
| インストールしたのに ✗ のまま | PATH は起動時のものを使う | アプリを再起動 |
| 起動が数秒遅い | R2 同期のネットワーク待ち | `R2_AUTOSYNC=0` |
| GUI で登録・削除したのに他の PC に届かない | この PC からの送信が失敗した（ネットワーク等）か、他の PC がまだ同期していない | この PC で `config-sync`（`R2_DEBUG=1` でコンソールから起動すると `auto-push skipped:` の行で理由が分かる）／他の PC で `config-sync` |
| 「保存に失敗しました（検証NG）」 | 同期ドライブの不調 | 同期状態を確認 → `doctor` |
| 「データセット … を開けません」（フォルダはある） | `myanalysis.toml` が空か壊れている | `doctor --dataset <ds>` で確認 → 0 バイトなら `--rescue --cache …`、それ以外はクラウドの版履歴で復元（`work_dir` と `format` を既定から変えていなければ、ファイルを削除するだけでもよい） |
| 「セッションを復元できません」 | `session.json` と `.bak` が両方壊れている | **タブを開いて保存する前に**、`doctor` やクラウドの版履歴で復元する（保存すると上書きされる） |
| 「analysis.py が空です」 | 書き込み失敗で 0 バイト | `recover-analysis <解析名> --dataset <ds>` |
| 「概要を保存できませんでした」 | `meta.json` と `.bak` が両方読めない（または書き込み失敗） | 少し待って再試行 → `doctor --dataset <ds>` → 0 バイトなら `--rescue --cache …` か版履歴。戻せなければ `meta.json` と `meta.json.bak` を両方削除する（概要と完了フラグは失われ、他は自動で作り直される） |
| チャットが消えた | チャットのファイルと `.bak` が両方壊れた | `doctor` の 0 バイト一覧 → `--rescue --cache …` か版履歴 |
| 毎回全履歴を送り直している | `data/llm_state/backend_sessions.json` が壊れている | `doctor` で「JSON として読めない」と出たら、GUI を終了してファイルを削除する |
| CLI の出力をファイルやパイプに流すと `UnicodeEncodeError` / 文字化け | Windows の文字コード（cp932） | `python -X utf8 -m …`、または環境変数 `PYTHONUTF8=1` |

エラーダイアログの「詳細はログ」は、`python tool.py`（コンソール付き）で起動したときのコンソール出力を指す。`run.bat` 起動では警告が残らない。ステータスバーの「保存に失敗しました」も 30 秒で消える。

**不具合を報告するときに添えるもの**: `doctor` の出力、`data/logs/gui-crash-*.log`、`python tool.py` で起動したときのコンソール出力、（必要なら）`data/llm_state/command_log.jsonl`。ログにはパスやデータセット名が含まれるので、添える前に確認する。
