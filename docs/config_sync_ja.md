# 複数 PC での設定同期

[← README に戻る](../README_ja.md)

Cloudflare R2（オブジェクトストレージ）を使って、PC 間で次の設定を同期する任意機能。設定しなければ何も起きない。

- **同期するもの**: `datasets.local.json`（全 PC のパス）、`models.toml`、`llm_backend/config.toml`。`.env` は `--include-env` を付けたときだけ送り、受け取る側では `.env.pulled` に保存する（`.env` は上書きしない）。
- **同期しないもの**: 計測データ・解析コード・`myanalysis.toml`（これらは同期ドライブで共有する）。ペルソナ定義。

## 1 台目のセットアップ

1. venv を有効にして `python -m pip install boto3`（GUI と同じ Python に入れる。別の Python に入れると自動同期が黙って何もしない）。
2. Cloudflare のダッシュボードで R2 のバケットを作る（**必ず非公開**）。
3. R2 の API トークンを作る。権限は「オブジェクトの読み取りと書き込み」（Object Read & Write）を**そのバケットに限定**する。表示される Access Key ID と Secret を控える。
4. `.env` に書く（**行末にコメントを付けない**）。

   ```
   R2_ENDPOINT_URL=https://<アカウントID>.r2.cloudflarestorage.com
   R2_BUCKET=<バケット名>
   R2_ACCESS_KEY_ID=<キー>
   R2_SECRET_ACCESS_KEY=<シークレット>
   ```

   endpoint にバケット名は含めない。任意で `R2_PREFIX`（既定 `config/`）。
5. `python -m llm_bridge config-sync --dry-run` で確認してから、`python -m llm_bridge config-sync`。

## 2 台目以降

1. README の [QuickStart](../README_ja.md#quickstart) の手順 1（インストール）だけを行い、`python -m pip install boto3`。
2. `.env` に 1 台目と同じ R2 の 4 行（`R2_PREFIX` を変えた場合はそれも）を書く。
3. **AI エンジンの設定（GUI の「適用」・＋ / －）や雛形のコピーをする前に** `python -m llm_bridge config-pull` を実行する（R2 を設定してから GUI を起動すれば、起動時の同期でも取り込まれる）。先に設定ファイルを作ると、更新時刻が新しい方が勝つ規則により、その最小限の内容が他の PC の設定を上書きしてしまう。
4. 各データセットを、この PC のパスで登録する（→ [別の PC で使う](usage_ja.md#別の-pc-で使う)）。

先に設定ファイルを作ってしまった場合は、同期する前に `models.toml` と `llm_backend/config.toml` を削除してから `config-pull` する。既に送ってしまった場合は、正しい設定を持つ PC で、**GUI を起動する前に**（起動時の同期で上書きされるため。または `.env` に `R2_AUTOSYNC=0` を入れてから）ファイルを編集して保存し直し、`config-sync` する。その PC が既に同期してしまっていれば正しい内容は失われているので、設定し直す。

## 自動同期と手動コマンド

- GUI の起動時と、CLI の `register-dataset` の後に自動で双方向同期する。GUI でのデータセットの登録と**登録の削除**は、その場で R2 へ送る（送信のみ。バックグラウンドで行い、完了は待たない）。設定ファイル（`models.toml` / `llm_backend/config.toml`）の変更は、この PC の次回起動時か次の送信、または `config-sync` のときに一緒に送られる。他の PC には、その PC の次回起動時（か `config-sync`）に届く。
- 失敗しても起動も登録・削除も止まらない（通常は数秒で打ち切り、何も表示しない。送れなかった変更は次回起動時の同期で送られる）。止めるには `.env` に `R2_AUTOSYNC=0`（起動時・登録・削除の後の自動同期をすべて止める）。
- 手動: `config-sync`（双方向）/ `config-push`（R2 側だけ書く）/ `config-pull`（ローカル側だけ書く）。どれもマージした結果を書くもので、片側で強制的に上書きするものではない。`--dry-run` で予定だけ表示。`--include-env` は `config-sync` / `config-push` だけ。
- 自動同期が効かないときは、まず `python -m llm_bridge config-sync --dry-run` でエラーと警告を確認する。自動同期（起動時・登録・削除の後）の例外を見るには `R2_DEBUG=1` を設定し、`python tool.py` をコンソールから起動する（`run.bat` では表示されない。起動時は `auto-sync skipped:`、GUI での登録・削除の後は `auto-push skipped:` で始まる行）。
- Cloudflare R2 専用。他の S3 互換ストレージは未検証。

## 競合の規則

- 登録簿は和集合で統合する。自分の PC のパスは常にローカルが優先。
- **登録の削除は「登録を削除」で行ったときだけ他の PC に伝播する。** `datasets.local.json` を手で編集して消したエントリは、次の同期で他の PC や R2 から復活する。削除の記録は `~/.myanalysis/config_share_state.json` にあるので、同期する前にこのファイルを消さない。
- 設定ファイルはファイル単位で、更新時刻が新しい方が勝つ。両方の PC で編集すると、古い方の変更は失われる。PC の時計を合わせておく。
- ホスト名が同じ PC を 2 台使わない（互いのパスを上書きし合う）。
- `config.toml` の PC 固有の値も他の PC に配られる。Claude / Codex の実行ファイルのパスを PC ごとに変えるなら、`[claude_code].bin` / `[codex].bin` を空にし、各 PC の `.env` の `CLAUDE_CODE_BIN` / `CODEX_BIN` で指定する（`bin` が空でないと環境変数は無視される。GUI で「Claude（PATH の CLI）」を選ぶと `bin = "claude"` が書かれる）。各セクションの `cwd` と `[pi].bin` は PC ごとに変えられないので、絶対パスを書かない。
- R2 同期を使う全 PC で、アプリを同じ版にそろえる（古い版は新しい形式の同期データを読めず、その PC では同期が止まる。起動時には何も表示されない）。

## R2 から .env を消すには

`--include-env` で送った `.env`（R2 の鍵や API キーを含む）は、自動では消えない。

1. 全 PC の GUI を終了する。
2. R2 のダッシュボードで `config/bundle.json`（`R2_PREFIX` を変えていればその下）を削除する。
3. 1 台で `config-sync` を実行する（`.env` を含まない bundle が作り直される）。
4. 各 PC の `.env.pulled` を削除し、送ってしまった鍵は再発行する。

bundle を削除しても、登録や設定は各 PC の同期で元に戻る。不要な登録は「登録を削除」で、設定は各 PC で直す。

> バケットには全 PC のホスト名とフルパスが入る。`--include-env` を使うと API キーなどが**平文**で保存される。受け取った `.env.pulled` はアプリからは読まれないので、必要な行を手で `.env` に写す。
