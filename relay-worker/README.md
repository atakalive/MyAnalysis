# ミーティング共有リレー（Issue #44）セットアップ

`View → ミーティング共有…` で会議相手をブラウザ（チャットドック）から自分の
チャットドックへライブ参加させる機能の**中継サーバ**。

Issue #44 で設計を見直し、**Cloudflare Worker + KV/R2 を廃止**して
**ローカル・インメモリ・リレー（`127.0.0.1`）+ cloudflared クイックトンネル**に
作り直した。共有データ（ライブビュー・チャット・presence）はすべて ephemeral で
ホスト GUI が動いて初めて存在する＝永続ストア（KV/R2）にポーリングで叩く必要が
無い。これで Cloudflare の KV write/list 課金次元が丸ごと消え、即時失効
（KV 伝播待ち無し）になる。

## 必要な設定（`.env`）

| 変数 | 中身 | 必須 |
|---|---|---|
| `RELAY_ADMIN_KEY` | ホストとして書き込むための秘密鍵 | **必須** |
| `RELAY_BASE_URL`  | legacy override（設定すると remote relay を指す） | 既定は**未設定** |

**`RELAY_ADMIN_KEY` のみ必須。** 公開トンネルは admin 含む全ルートをインターネットに
晒すため、弱鍵・推測可能鍵は admin 乗っ取りリスクになる。**十分な長さのランダム鍵**を使うこと：

```
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

生成値を `.env` の `RELAY_ADMIN_KEY=…` に書く（gitignore 済み）。

`RELAY_BASE_URL` は**未設定が既定**。設定するとローカルサーバ + トンネルを起動せず、
その URL を remote relay として使う legacy override モードになる（旧構成の延命用）。

## 前提バイナリ: `cloudflared`

クイックトンネルには `cloudflared` が必要（PATH 上、または環境変数 `CLOUDFLARED_BIN`
で明示）。Node/wrangler とは別バイナリなので個別に入れる：

```
winget install cloudflare.cloudflared
```

## 動作

共有開始（GUI またはCLI/llm_bridge）で：

1. ローカル・インメモリ・リレーが `127.0.0.1:<エフェメラルポート>` に起動（外部直アクセス不可）。
2. cloudflared クイックトンネルが起動し、毎回ランダムな `*.trycloudflare.com` URL を払い出す。
   この URL がゲスト配布トークンに入る（**URL は都度変わる／SLA 無し**）。
3. ゲストはトークン内の `trycloudflare.com` URL を開いて入室。チャット双方向・ライブビュー・
   公開トグル・即時失効はすべて従来どおり。

## 旧 Cloudflare Worker の退役（手動）

既に `worker.js` を Cloudflare にデプロイ済みなら、`relay-worker/` で

```
wrangler delete
```

を実行して workers.dev の残留トラフィック・課金を止める（**人間が実施する運用手順**）。

`wrangler.toml` / `worker.js` のコードは**参照用に残置**（移植元の Single Source of
Truth）。再デプロイは不要。

## トラブルシュート

- **「トンネル起動に失敗しました」** → `cloudflared` が PATH 上に無いか、起動が
  タイムアウト。`winget install cloudflare.cloudflared` 後、新しいシェルで再試行。
  別パスにあるなら `CLOUDFLARED_BIN` で指定。
- **開始ボタンが無効/赤字のまま** → `.env` に `RELAY_ADMIN_KEY` が未設定。GUI を
  再起動したか（`.env` は起動時に一度だけ読まれる）。
- **トークンの URL が毎回変わる** → クイックトンネルの仕様（固定 URL が必要なら
  named tunnel へ昇格）。
