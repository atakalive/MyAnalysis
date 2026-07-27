# ミーティング共有リレー（Issue #44）セットアップ

`View → ミーティング共有…` で会議相手をブラウザ（チャットドック）から自分の
チャットドックへライブ参加させる機能の**中継サーバ**。

Issue #44 で設計を見直し、**Cloudflare Worker + KV/R2 を廃止**して
**ローカル・インメモリ・リレー（`127.0.0.1`）+ 公開トンネル**に作り直した。共有データ
（ライブビュー・チャット・presence）はすべて ephemeral でホスト GUI が動いて初めて存在する＝
永続ストア（KV/R2）にポーリングで叩く必要が無い。これで Cloudflare の KV write/list 課金次元が
丸ごと消え、即時失効（KV 伝播待ち無し）になる。

## 公開トンネル（cloudflared named tunnel）

ローカルリレーは**既定では loopback（`127.0.0.1`）のみに bind** するので、外部公開は公開トンネル経由。
（LAN リンク ON 時のみ、同一リレーを `0.0.0.0` にもバインドし LAN 直結の第2リンクを出す。後述「LAN 直結リンク」参照。）
外部公開は Cloudflare **named** tunnel 一本（[../meeting/tunnel.py](../meeting/tunnel.py)）。自分の委譲
ドメインで**固定** `https://<hostname>` を発行する。

## 必要な設定（`.env`）

| 変数 | 中身 | 必須 |
|---|---|---|
| `RELAY_ADMIN_KEY` | ホストとして書き込むための秘密鍵 | **必須** |
| `CLOUDFLARE_TUNNEL_NAME` | tunnel 名 or UUID | **必須** |
| `CLOUDFLARE_TUNNEL_HOSTNAME` | `route dns` した固定ホスト名 | **必須** |
| `CLOUDFLARED_BIN` | cloudflared が PATH に無いとき | 任意 |
| `CLOUDFLARE_TUNNEL_CRED` | 認証情報ファイル（別PCで `login` 省略用） | 任意 |
| `RELAY_LAN_HOST` | LAN URL のホスト初期値／CLI 単独 LAN 起動時のホスト供給源（Issue #85） | 既定**未設定** |
| `RELAY_LAN_PORT` | LAN リンクの固定ポート要求（**bare integer 必須**）。**衝突時は ephemeral に落とさず `OSError`＝起動失敗として通知**。未設定/非整数は ephemeral | 既定**未設定** |

**`RELAY_ADMIN_KEY` のみ無条件必須。** 公開トンネルは admin 含む全ルートをインターネットに
晒すため、弱鍵・推測可能鍵は admin 乗っ取りリスクになる。**十分な長さのランダム鍵**を使うこと：

```
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

生成値を `.env` の `RELAY_ADMIN_KEY=…` に書く（gitignore 済み）。外部公開には
`CLOUDFLARE_TUNNEL_NAME` と `CLOUDFLARE_TUNNEL_HOSTNAME` も必須（未設定だと共有開始時に
`tunnel_failed`。LAN リンクは cloudflared 非依存で従来どおり使える）。

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

PATH に通らない場合は新しいシェルで再試行、または `CLOUDFLARED_BIN` でフルパス指定。

### 2. login → create → route dns

```
cloudflared tunnel login                                  # ブラウザでゾーンを認可 → ~/.cloudflared/cert.pem
cloudflared tunnel create myanalysis-relay                # → UUID と ~/.cloudflared/<UUID>.json
cloudflared tunnel route dns myanalysis-relay relay.example.com
```

`route dns` がゾーンに `relay.example.com → <UUID>.cfargotunnel.com`（proxied）の CNAME を作る。
以後ホスト名は固定。`.env` に:

```
CLOUDFLARE_TUNNEL_NAME=myanalysis-relay
CLOUDFLARE_TUNNEL_HOSTNAME=relay.example.com
```

> **別 PC で使う場合**: `tunnel run <NAME>` は名前→UUID 解決に `cert.pem`（`tunnel login` の産物）が要る。
> 別 PC で `login` を省くなら、`CLOUDFLARE_TUNNEL_NAME` に **UUID** を入れ、`<UUID>.json` をその PC に置いて
> `CLOUDFLARE_TUNNEL_CRED` でフルパス指定する（cert.pem 不要）。

## 動作

共有開始（GUI または CLI/llm_bridge）で：

1. ローカル・インメモリ・リレーが起動（既定は `127.0.0.1:<エフェメラルポート>` の loopback のみ＝外部直アクセス不可。
   LAN リンク ON 時のみ `0.0.0.0:<ポート>` にもバインドし LAN 直結を許す）。
2. cloudflared がトンネルを起動。
   `cloudflared tunnel run --url http://127.0.0.1:<port> <name>` で **固定**ホスト名
   `https://<CLOUDFLARE_TUNNEL_HOSTNAME>` を公開（URL は毎回同じ）。`--url` でアドホック ingress を
   渡すので、ephemeral なローカルポートを config ファイルに焼く必要が無い。
3. ゲストはトークン内の公開 URL を開いて入室。チャット双方向・ライブビュー・公開トグル・即時失効は従来どおり。

ホスト自身は（LAN の ON/OFF に関わらず）常に `127.0.0.1` のローカルリレーに話す（Cloudflare エッジを通らない）。
エッジを通るのはゲスト（実ブラウザ）のみ ＝ ホスト側 PC の DNS が当該ホスト名を引けなくても機能には影響しない。

## LAN 直結リンク（Issue #85）

組織ネットワークの DNS がトンネルのホスト名を NXDOMAIN で解決できない環境向けに、内部ゲストをトンネル経由でなく
**ホスト PC へ LAN 直結**させる第2リンクを併発できる。共有ダイアログの「LAN リンクも出す」を ON にすると
（既定 OFF）、同一 channel/secret のまま同一リレーを `0.0.0.0` にもバインドし、`base_url` をホストの
LAN URL にした第2トークンを生成する。外部リンク（トンネル https）とLAN リンク（LAN http）は同時に有効で、
内外ゲストが同一会議に混在できる。CLI は `python -m llm_bridge meeting-start lan=true`＋`meeting-lan-link`。

**配布はフルリンクのみ（素トークンは配らない）**: LAN リンクは http。https ページから http を fetch すると
ブラウザが mixed-content で強制ブロックするため、LAN 内ゲストは **http のディープリンク
（`http://<ip>:<port>/#token=…`）でページごと開く**必要がある（ページ origin を http にする）。よって
GitLab Pages の https 専用ページに素トークンを貼る既存運用はLAN 内では使えない。

**固定ポート要求（`RELAY_LAN_PORT`）**: 告知済み URL / Firewall 規則 / ゲスト案内が固定ポート前提のとき、
黙って別ポートに変わるとゲストが到達不能になる。よって**衝突時は ephemeral に落とさず `OSError`＝起動失敗**として
通知する（別の `RELAY_LAN_PORT` で再試行）。稀な自己衝突（旧 loopback の ephemeral ポートが偶然要求固定ポートを
保持）でも同様にクリーン失敗し、**旧 loopback サーバは無傷で残る**（旧を先に畳んで retry はしない）。未設定/非整数は ephemeral。

### セキュリティ（`0.0.0.0` バインドの露出）

1節にまとめて明記する:

- **(a)** `0.0.0.0` はグローバル IP を持つホストではネットワークの firewall 次第で**インターネットからも到達し得る**
  （SoftEther 仮想アダプタや他 NIC にも露出）。門番は per-meeting secret（`token_urlsafe(32)`）＋ admin_key ＋ TTL ＋
  heartbeat-grace。**public バインドは共有中のみ**（`stop`/`expired`/`start` 失敗の全経路で畳む）。
- **(b)** LAN は平文 http なので secret はローカルネットワーク上で平文で流れる（配布はフルリンクのみ）。
- **(c)** ゲストは HTTPS-Only を切って http フルリンクで開く（企業ポリシーで ON 固定だと開けない）。
- **(d)** Windows Firewall は inbound を**プログラム単位（`pythonw.exe`）で許可 + ephemeral ポート**推奨。
- **将来の締め（follow-up #88）**: `0.0.0.0` でなく「loopback ＋ LAN IP 専用」の2ソケットを同一 `RelayState` で
  待つと SoftEther/グローバル面を除外できる。

## トラブルシュート

- **「トンネル起動に失敗しました」**（cloudflared）→ 共有ウィンドウのログに原因が出る:
  - `cloudflared not found` → `winget install cloudflare.cloudflared` 後、新しいシェルで再試行。別パスなら `CLOUDFLARED_BIN`。
  - `CLOUDFLARE_TUNNEL_NAME/HOSTNAME not set` → `.env` 未設定。GUI を再起動したか（`.env` は起動時に一度だけ読まれる）。
  - `did not register within …s` → 認証情報/ネットワーク不良。`cloudflared tunnel run <name>` を手で叩いて切り分け。
- **ゲストが Cloudflare エラー 1033** → `tunnel route dns` 未実行、またはホスト名と tunnel の紐付けズレ。
- **開始ボタンが無効/赤字のまま** → `.env` に `RELAY_ADMIN_KEY` が未設定。GUI を再起動したか確認。
