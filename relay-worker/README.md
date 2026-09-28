# ミーティング共有リレー セットアップ

`表示 → ミーティング共有…` で会議相手をブラウザ（チャットドック）から自分の
チャットドックへライブ参加させる機能の**中継サーバ**。

> **セキュリティ注意**: 参加リンクを持つ人は誰でもチャットにメッセージを送れ、そのメッセージは
> **承認なしでホストの AI エージェントを動かす**（エージェントは既定でホストのユーザー権限で動作する）。
> 参加リンクは信頼できる相手にだけ渡し、会議が終わったら共有を停止すること。

構成は**ローカル・インメモリ・リレー（`127.0.0.1`）+ 公開トンネル**。共有データ
（ライブビュー・チャット・presence）はすべて ephemeral でホスト GUI が動いて初めて存在し、
永続ストアには保存しない（共有を止めれば即時失効）。

## 公開トンネル（cloudflared named tunnel）

ローカルリレーは**既定では loopback（`127.0.0.1`）のみに bind** するので、外部公開は公開トンネル経由。
（LAN リンク ON 時のみ、同一リレーを `0.0.0.0` にもバインドし LAN 直結の第2リンクを出す。後述「LAN 直結リンク」参照。）
外部公開は Cloudflare **named** tunnel 一本（[../meeting/tunnel.py](../meeting/tunnel.py)）。自分の委譲
ドメインで**固定** `https://<hostname>` を発行する。

## 必要な設定（`.env`）

| 変数 | 中身 | 必須 |
|---|---|---|
| `RELAY_ADMIN_KEY` | ホストとして書き込むための秘密鍵 | **必須** |
| `CLOUDFLARE_TUNNEL_NAME` | tunnel 名 or UUID | 外部リンクに必須 |
| `CLOUDFLARE_TUNNEL_HOSTNAME` | `route dns` した固定ホスト名 | 外部リンクに必須 |
| `CLOUDFLARED_BIN` | cloudflared が PATH に無いとき | 任意 |
| `CLOUDFLARE_TUNNEL_CRED` | 認証情報ファイル（別PCで `login` 省略用） | 任意 |
| `RELAY_LAN_HOST` | LAN URL のホスト初期値／CLI 単独 LAN 起動時のホスト供給源 | 既定**未設定** |
| `RELAY_LAN_PORT` | LAN リンクの固定ポート要求（**bare integer 必須**）。**衝突時は ephemeral に落とさず `OSError`＝起動失敗として通知**。未設定/非整数は ephemeral | 既定**未設定** |

**`RELAY_ADMIN_KEY` のみ無条件必須。** 公開トンネルは admin 含む全ルートをインターネットに
晒すため、弱鍵・推測可能鍵は admin 乗っ取りリスクになる。**十分な長さのランダム鍵**を使うこと：

```
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

生成値を `.env` の `RELAY_ADMIN_KEY=…` に書く（gitignore 済み）。外部公開には
`CLOUDFLARE_TUNNEL_NAME` と `CLOUDFLARE_TUNNEL_HOSTNAME` も必須（未設定だと共有開始時に
`tunnel_failed`。LAN リンクは cloudflared 非依存なので、トンネル未設定でも使える）。

## cloudflared named tunnel のワンタイム設定

固定 URL のための一度きりの準備（ブラウザ認証を含む人間の作業）。

### 0. 前提: 自分のドメインを Cloudflare に委譲

ネームサーバ（NS）を Cloudflare に向けられるドメイン（例 `example.com`）を用意し、その NS を Cloudflare に
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
3. ゲストは参加リンク（`https://<ホスト名>/#token=…`）を開いて入室。ゲスト用ページ（チャットドック）は
   ホストのリレー自身が `https://<ホスト名>/` で配信するので、別途ページをホストする必要は無い
   （GitLab Pages 等への配置は任意で、不要）。チャット双方向・ライブビュー・公開トグル・即時失効に対応。

ホスト自身は（LAN の ON/OFF に関わらず）常に `127.0.0.1` のローカルリレーに話す（Cloudflare エッジを通らない）。
エッジを通るのはゲスト（実ブラウザ）のみ ＝ ホスト側 PC の DNS が当該ホスト名を引けなくても機能には影響しない。

## LAN 直結リンク

組織ネットワークの DNS がトンネルのホスト名を NXDOMAIN で解決できない環境向けに、内部ゲストをトンネル経由でなく
**ホスト PC へ LAN 直結**させる第2リンクを併発できる。共有ダイアログの「LAN リンクも出す」を ON にすると
（既定 OFF）、同一 channel/secret のまま同一リレーを `0.0.0.0` にもバインドし、`base_url` をホストの
LAN URL にした第2トークンを生成する。外部リンク（トンネル https）とLAN リンク（LAN http）は同時に有効で、
内外ゲストが同一会議に混在できる。CLI は `python -m llm_bridge window meeting-start lan=true --wait`＋
`python -m llm_bridge window meeting-lan-link --wait`。

**配布はフルリンクのみ（素トークンは配らない）**: LAN リンクは http。https ページから http を fetch すると
ブラウザが mixed-content で強制ブロックするため、LAN 内ゲストは **http のディープリンク
（`http://<ip>:<port>/#token=…`）でページごと開く**必要がある（ページ origin を http にする）。よって
https のページ（外部リンクのページや任意の静的配置）に素トークンを貼る方法はLAN 内では使えない。

**固定ポート要求（`RELAY_LAN_PORT`）**: 告知済み URL / Firewall 規則 / ゲスト案内が固定ポート前提のとき、
黙って別ポートに変わるとゲストが到達不能になる。よって**衝突時は ephemeral に落とさず `OSError`＝起動失敗**として
通知する（別の `RELAY_LAN_PORT` で再試行）。稀な自己衝突（旧 loopback の ephemeral ポートが偶然要求固定ポートを
保持）でも同様にクリーン失敗し、**旧 loopback サーバは無傷で残る**（旧を先に畳んで retry はしない）。未設定/非整数は ephemeral。

### セキュリティ（`0.0.0.0` バインドの露出）

1節にまとめて明記する:

- **(a)** `0.0.0.0` はグローバル IP を持つホストではネットワークの firewall 次第で**インターネットからも到達し得る**
  （VPN 等の仮想アダプタや他 NIC にも露出）。門番は per-meeting secret（`token_urlsafe(32)`）＋ admin_key ＋ TTL ＋
  heartbeat-grace。**public バインドは共有中のみ**（`stop`/`expired`/`start` 失敗の全経路で畳む）。
- **(b)** LAN は平文 http なので secret はローカルネットワーク上で平文で流れる（配布はフルリンクのみ）。
- **(c)** ゲストは HTTPS-Only を切って http フルリンクで開く（企業ポリシーで ON 固定だと開けない）。
- **(d)** Windows Firewall は inbound を**プログラム単位（`pythonw.exe`）で許可 + ephemeral ポート**推奨。

## トラブルシュート

- **「トンネル起動に失敗しました」**（cloudflared）→ 共有ウィンドウには詳細な原因は出ない。
  `cloudflared tunnel run <name>` を手で実行するとエラーが確認できる。よくある原因:
  - cloudflared が見つからない → `winget install cloudflare.cloudflared` 後、新しいシェルで再試行。別パスなら `CLOUDFLARED_BIN`。
  - `CLOUDFLARE_TUNNEL_NAME/HOSTNAME` 未設定 → `.env` を確認。GUI を再起動したか（`.env` は起動時に一度だけ読まれる）。
  - 登録がタイムアウトする → 認証情報/ネットワーク不良。
- **ゲストが Cloudflare エラー 1033** → 多くはホストが現在共有していない（停止・期限切れ・アプリ終了）。
  共有中なのに出る場合は `tunnel route dns` 未実行、またはホスト名と tunnel の紐付けズレ。
- **開始ボタンが無効/赤字のまま** → `.env` に `RELAY_ADMIN_KEY` が未設定。GUI を再起動したか確認。
