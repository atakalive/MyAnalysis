# ミーティング共有リレー（Issue #42）セットアップ

`View → ミーティング共有…` で会議相手をブラウザ（チャットドック）から自分の
チャットドックへライブ参加させる機能の**中継サーバ**。相手のブラウザと自分の PC
を HTTP で橋渡しするため、この Cloudflare Worker を**自分の Cloudflare アカウントに
デプロイする必要がある**。デプロイするまで GUI は「.env に RELAY_BASE_URL と
RELAY_ADMIN_KEY を設定してください」と表示し、開始ボタンは無効。

`.env` の 2 項目は「どこかの既存値を写す」ものではなく、**このデプロイ作業の過程で
決まる値**：

| 変数 | 中身 | いつ決まるか |
|---|---|---|
| `RELAY_ADMIN_KEY` | ホストとして書き込むための秘密鍵（自分で決める） | 手順 4。同じ値を Worker secret にも登録 |
| `RELAY_BASE_URL`  | デプロイした Worker の URL | 手順 5 の `wrangler deploy` 成功時に確定 |

## 構成

- `worker.js` — Cloudflare Worker 本体。KV（メタ/メッセージ/presence）+ R2（view PNG）。
- `chatdock.html` — ゲスト用 UI。Worker が `GET /` で配信（Text モジュールとして bundle）。
- `wrangler.toml` — デプロイ設定。KV namespace id と R2 バケットの binding を持つ。

## コスト

heartbeat(~20s) + presence(~20s) の KV write が無料枠（~1000 writes/日）を会議数時間で
超える。**短時間デモは無料枠で可、常用は Workers Paid（$5/月）推奨**。READ/LIST・
リクエスト数・R2 は有料枠の無料分で収まる。

## 前提

- Cloudflare アカウント（R2 を使っているなら取得済み）。
- Node.js（wrangler が必要とする）。未導入なら `winget install OpenJS.NodeJS.LTS`。
- wrangler：`npm i -g wrangler`（または各コマンドを `npx wrangler …` に読み替え）。

以降のコマンドは `relay-worker/` ディレクトリで実行する。

## 手順

### 1. wrangler ログイン
```
wrangler login
```
ブラウザが開き Cloudflare アカウントへ認証する（要クリック）。

### 2. KV namespace を作成して id を wrangler.toml に焼く
```
wrangler kv namespace create RELAY_KV
```
出力された `id`（例 `id = "abc123…"`）を `wrangler.toml` の
`REPLACE_WITH_KV_NAMESPACE_ID` と置換する。この id は秘密ではないのでコミットしてよい。

### 3. R2 バケットを作成
```
wrangler r2 bucket create REPLACE_WITH_BUCKET
```
バケット名は `wrangler.toml` の `bucket_name` と一致必須。任意で Cloudflare ダッシュボード
にて `view/` 配下を ~1 日で expire する lifecycle ルールを設定すると孤児 PNG が自動回収
される（`DELETE /admin/channel` は view を消さない設計のため）。

### 4. 管理鍵を Worker secret として登録
鍵は `.env` に既に生成済み（`RELAY_ADMIN_KEY=…`）。同じ値を Worker 側にも入れる：
```
wrangler secret put RELAY_ADMIN_KEY
```
プロンプトに `.env` の `RELAY_ADMIN_KEY` の値を貼り付ける。

> secret は `wrangler.toml` の `[vars] RELAY_ADMIN_KEY` を上書きするので、`wrangler.toml`
> 側はプレースホルダのままでよい（**実鍵を `wrangler.toml` に書かないこと**。このファイルは
> コミットされる）。新しい鍵を作り直したいときは
> `python -c "import secrets; print(secrets.token_urlsafe(32))"`。

### 5. デプロイ
```
wrangler deploy
```
成功すると `https://myanalysis-relay.<account>.workers.dev` が表示される。

### 6. `.env` に URL を記入
リポジトリ直下の `.env`（`.env.example` ではない）の `RELAY_BASE_URL` 行の行頭 `#` を外し、
手順 5 の URL を入れる：
```
RELAY_BASE_URL=https://myanalysis-relay.<account>.workers.dev
```
値はクォート不要。`.env` は gitignore 済みなので鍵を書いて安全。

### 7. GUI を再起動
`.env` は起動時に一度だけ読まれる（`tool.py` の `load_env()`）。ホットリロードでは
反映されないので**プロセス再起動が必須**。再起動後 `View → ミーティング共有…` の赤字が
消え、開始ボタンが有効になれば設定完了。

## 動作確認

1. GUI で `View → ミーティング共有…` → 開始 → 招待トークンが生成される。
2. 別ブラウザ（できれば別端末）で `RELAY_BASE_URL` を開き、トークンと表示名を入力して入室。
3. ゲストの発言がホストのチャットへ user turn として注入され、ゲスト側にホスト応答と
   アクティブタブの view が表示されることを確認。

## トラブルシュート

- **開始ボタンが無効/赤字のまま** → `.env` の 2 変数が両方セットされ、行頭 `#` が外れているか。
  GUI を再起動したか（ホットリロード不可）。
- **共有開始はできるがゲストが入れない/401** → `.env` の `RELAY_ADMIN_KEY` と
  `wrangler secret put` で入れた値が不一致。手順 4 をやり直す。
- **view が出ない/接続不安定** → URL 末尾の余分なスラッシュ、または無料枠の KV write
  上限超過。Workers Paid を検討。
- **`wrangler` が見つからない** → Node 未導入。`winget install OpenJS.NodeJS.LTS` 後、
  新しいシェルを開いて `npm i -g wrangler`。
