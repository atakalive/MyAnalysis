# ミーティング共有リレー（Issue #44）セットアップ

`View → ミーティング共有…` で会議相手をブラウザ（チャットドック）から自分の
チャットドックへライブ参加させる機能の**中継サーバ**。

Issue #44 で設計を見直し、**Cloudflare Worker + KV/R2 を廃止**して
**ローカル・インメモリ・リレー（`127.0.0.1`）+ 公開トンネル**に作り直した。共有データ
（ライブビュー・チャット・presence）はすべて ephemeral でホスト GUI が動いて初めて存在する＝
永続ストア（KV/R2）にポーリングで叩く必要が無い。これで Cloudflare の KV write/list 課金次元が
丸ごと消え、即時失効（KV 伝播待ち無し）になる。

## 公開トンネルの選択（`RELAY_TUNNEL`、既定 `cloudflared`）

ローカルリレーは `127.0.0.1` にしか bind しないので、外部公開は公開トンネル経由。プロバイダは
`RELAY_TUNNEL` で差し替え可能（[../meeting/tunnel.py](../meeting/tunnel.py)）:

| 値 | 中身 | URL |
|---|---|---|
| `cloudflared`（既定） | Cloudflare **named** tunnel（自分の委譲ドメイン） | **固定** `https://<hostname>` |
| `pinggy` | SSH/443 リバーストンネル | 都度変化（無料は約60分で切断） |
| `tailscale` | Tailscale Funnel | 固定 `*.ts.net`（ゲスト回線次第で到達性不安定） |

## 必要な設定（`.env`）

| 変数 | 中身 | 必須 |
|---|---|---|
| `RELAY_ADMIN_KEY` | ホストとして書き込むための秘密鍵 | **必須** |
| `RELAY_TUNNEL` | トンネル選択（既定 `cloudflared`） | 任意 |
| `CLOUDFLARE_TUNNEL_NAME` | tunnel 名 or UUID | cloudflared 時**必須** |
| `CLOUDFLARE_TUNNEL_HOSTNAME` | `route dns` した固定ホスト名 | cloudflared 時**必須** |
| `CLOUDFLARED_BIN` | cloudflared が PATH に無いとき | 任意 |
| `CLOUDFLARE_TUNNEL_CRED` | 認証情報ファイル（別PCで `login` 省略用） | 任意 |
| `RELAY_BASE_URL` | legacy override（設定すると remote relay を直叩き） | 既定**未設定** |

**`RELAY_ADMIN_KEY` のみ無条件必須。** 公開トンネルは admin 含む全ルートをインターネットに
晒すため、弱鍵・推測可能鍵は admin 乗っ取りリスクになる。**十分な長さのランダム鍵**を使うこと：

```
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

生成値を `.env` の `RELAY_ADMIN_KEY=…` に書く（gitignore 済み）。`RELAY_BASE_URL` は**未設定が既定**。
設定するとローカルサーバ + トンネルを起動せず、その URL を remote relay として使う legacy override に
なる（旧構成の延命用）。

## cloudflared named tunnel のワンタイム設定

固定 URL のための一度きりの準備（ブラウザ認証を含む人間の作業）。

### 0. 前提: 自分のドメインを Cloudflare に委譲

DigitalPlat FreeDomain 等でドメイン（例 `example.com`）を取得し、その NS を Cloudflare に
委譲する（Cloudflare にゾーン追加 → 払い出された 2 つの NS をレジストラ側に設定）。委譲が済むと
`tunnel route dns` がゾーンに CNAME を自動作成できる（NS が他社のままだと `<UUID>.cfargotunnel.com`
は公開解決されず named tunnel は機能しない）。

### 1. cloudflared 導入

```
winget install cloudflare.cloudflared
```

Node/wrangler とは別バイナリ。PATH に通らない場合は新しいシェルで再試行、または `CLOUDFLARED_BIN`
でフルパス指定。

### 2. login → create → route dns

```
cloudflared tunnel login                                  # ブラウザでゾーンを認可 → ~/.cloudflared/cert.pem
cloudflared tunnel create myanalysis-relay                # → UUID と ~/.cloudflared/<UUID>.json
cloudflared tunnel route dns myanalysis-relay relay.example.com
```

`route dns` がゾーンに `relay.example.com → <UUID>.cfargotunnel.com`（proxied）の CNAME を作る。
以後ホスト名は固定。`.env` に:

```
RELAY_TUNNEL=cloudflared
CLOUDFLARE_TUNNEL_NAME=myanalysis-relay
CLOUDFLARE_TUNNEL_HOSTNAME=relay.example.com
```

> **別 PC で使う場合**: `tunnel run <NAME>` は名前→UUID 解決に `cert.pem`（`tunnel login` の産物）が要る。
> 別 PC で `login` を省くなら、`CLOUDFLARE_TUNNEL_NAME` に **UUID** を入れ、`<UUID>.json` をその PC に置いて
> `CLOUDFLARE_TUNNEL_CRED` でフルパス指定する（cert.pem 不要）。

## 動作

共有開始（GUI または CLI/llm_bridge）で：

1. ローカル・インメモリ・リレーが `127.0.0.1:<エフェメラルポート>` に起動（外部直アクセス不可）。
2. `RELAY_TUNNEL` のプロバイダがトンネルを起動。cloudflared なら
   `cloudflared tunnel run --url http://127.0.0.1:<port> <name>` で **固定**ホスト名
   `https://<CLOUDFLARE_TUNNEL_HOSTNAME>` を公開（URL は毎回同じ）。`--url` でアドホック ingress を
   渡すので、ephemeral なローカルポートを config ファイルに焼く必要が無い。
3. ゲストはトークン内の公開 URL を開いて入室。チャット双方向・ライブビュー・公開トグル・即時失効は従来どおり。

ホスト自身は `127.0.0.1` のローカルリレーに話す（Cloudflare エッジを通らない）。エッジを通るのは
ゲスト（実ブラウザ）のみ ＝ ホスト側 PC の DNS が当該ホスト名を引けなくても機能には影響しない。

## 旧 Cloudflare Worker の退役（手動）

既に `worker.js` を Cloudflare にデプロイ済みなら、`relay-worker/` で `wrangler delete` を実行して
workers.dev の残留トラフィック・課金を止める（**人間が実施する運用手順**）。`wrangler.toml` /
`worker.js` のコードは**参照用に残置**（移植元の Single Source of Truth）。再デプロイは不要。

## トラブルシュート

- **「トンネル起動に失敗しました」**（cloudflared）→ 共有ウィンドウのログに原因が出る:
  - `cloudflared not found` → `winget install cloudflare.cloudflared` 後、新しいシェルで再試行。別パスなら `CLOUDFLARED_BIN`。
  - `CLOUDFLARE_TUNNEL_NAME/HOSTNAME not set` → `.env` 未設定。GUI を再起動したか（`.env` は起動時に一度だけ読まれる）。
  - `did not register within …s` → 認証情報/ネットワーク不良。`cloudflared tunnel run <name>` を手で叩いて切り分け。
- **ゲストが Cloudflare エラー 1033** → `tunnel route dns` 未実行、またはホスト名と tunnel の紐付けズレ。
- **開始ボタンが無効/赤字のまま** → `.env` に `RELAY_ADMIN_KEY` が未設定。GUI を再起動したか確認。
- **（pinggy）トークンの URL が毎回変わる** → クイック系トンネルの仕様。固定 URL が要るなら
  cloudflared named tunnel を使う（既定）。
