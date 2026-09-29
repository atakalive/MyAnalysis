# AI エンジンの設定

[← README に戻る](../README_ja.md)

最初の設定は README の [AI エンジン](../README_ja.md#ai-エンジン) だけで足りる。この文書は、別のエンジンを使う・チャットごとに変える・設定ファイルを手で書くときの参照用。

## エンジン一覧

| 利用方法（エンジン） | `[backend].name` | `models.toml` のセクション | 解析 | 必要なもの |
|---|---|---|---|---|
| Claude（VS Code 同梱エンジン）**推奨** | `claude`（`bin` 空） | `[claude_code]` | ○ | VS Code / Cursor / Windsurf の Claude Code 拡張にサインイン（見つからなければ PATH 上の `claude` を使う）。Windows では Git for Windows を推奨（MyAnalysis のエージェントへの指示は Bash ツールを前提にしている） |
| Claude（PATH の CLI） | `claude`（`bin` 指定） | `[claude_code]` | ○ | `npm i -g @anthropic-ai/claude-code` → `claude` でログイン |
| Codex CLI（OpenAI） | `codex` | `[codex]` | ○ | `npm i -g @openai/codex` → `codex login` |
| pi コーディングエージェント | `pi` | `[pi]` | ○ | Node.js 22.19+、pi 0.67.4 以上、`npm i -g @earendil-works/pi-coding-agent` → `pi` を起動して `/login`。Windows では Git Bash |
| OpenAI 互換 HTTP | `openai` | `[openai-compat]` | ×（GUI 操作のみ） | `.env` の `OPENAI_BASE_URL` 等 |
| モック（動作確認） | `mock` | — | × | 不要 |

## 設定のしかた

- **設定 → バックエンド/モデル設定…**: 「利用方法（エンジン）」「モデル」「プロバイダ」（pi のみ）を選んで **適用**。全チャット（「このチャットのモデル…」で個別に設定したチャットを除く）の次の送信から反映される（生成中の応答は古い設定のまま完了する）。
  - モデル欄は自由入力。＋ / － で候補リストに追加・削除できる（`models.toml` に即保存）。空欄にするとエンジン側の既定モデルになる（OpenAI 互換では `OPENAI_MODEL`、それも無ければ `gpt-4o-mini`）。
  - **疎通確認** は実際に 1 ターン分を送って応答を確かめる。**適用** は疎通確認をしない。
  - thinking / effort は GUI に項目が無い。`models.toml` を手で編集し、再起動するか GUI で **適用** し直す（→ [設定ファイル](#設定ファイル手動設定)）。
- **チャットごとの設定**: チャットタブを右クリック → **このチャットのモデル…**。「全体設定に従う」を外すと、そのチャットだけ別のエンジン・モデルにできる（チャットと一緒に保存・同期される）。

## モデルの指定

モデル欄は空でよく、そのエンジンの既定モデルが使われる。

| エンジン | モデル欄に入れるもの | ID の調べ方 | 追加の設定（`models.toml`） |
|---|---|---|---|
| Claude | `opus` / `sonnet` などのエイリアス、または正式なモデル名 | Claude Code の `/model` に出る名前 | `thinking` = `enabled` / `adaptive` / `disabled`、`effort` = `low` / `medium` / `high` / `xhigh` / `max` / `ultracode` |
| Codex | `gpt-5.5` などカタログの ID | ダイアログの候補（Codex の既定カタログ） | `effort` = `minimal` / `low` / `medium` / `high` |
| pi | プロバイダ（`openai-codex` / `github-copilot` / `llama.cpp`）とモデル ID | `pi --list-models`。`llama.cpp` は llama-server に読み込んであるモデルだけが出る | なし（`thinking` / `effort` は使わない） |
| OpenAI 互換 HTTP | サーバー側のモデル名 | Ollama なら `ollama list`、それ以外はサーバーの一覧 | なし |

ダイアログの候補リストは ＋ / － で編集でき、`models.toml` の `model_choices` / `provider_choices` に保存される。

## バックエンドの状況ウィンドウ

**設定 → バックエンドの状況…** で、対応エンジン全部の導入状況を一覧できる（いま選んでいるエンジンとは無関係）。CLI では `python -m llm_bridge engines`。

- 列: ドライバ / 前提（node と npm）/ 導入（バージョン）/ 認証 / 操作。記号は ✓ = OK、✗ = 無い、? = 確認できなかった（タイムアウト等。未導入という意味ではない）、— = 対象外。
- **インストール / 更新**: どちらも `npm i -g <パッケージ>` を実行する。VS Code 同梱エンジンの行にはボタンが無い（拡張の更新で入手する）。「前提」が ✗（Node.js / npm が無い、pi には Node.js が古い）の行には出ないので、先に Node.js を入れる。「導入」が ? の行にも出ない（**再確認** するか、コマンドを手で実行して確かめる）。
- **ログイン**: 導入済みの Claude（PATH の CLI）/ pi / Codex の行に出る。Windows では新しいコンソールを開く。他の OS では実行すべきコマンドをログ欄に表示する。
- **疎通確認**: 実際にエンジンへ 1 ターン送って応答を確かめる。ウィンドウを開く・**再確認** は導入状況を調べるだけで、推論は走らない。
- 判定には認証ファイルを変更しない読み取り専用のコマンドだけを使う。
- インストール後も ✗ のままなら、アプリを再起動する（PATH は起動時のものを使うため）。
- Claude の「認証」列は `claude auth status` の結果。取れないときは `~/.claude/.credentials.json` と環境変数（`ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN`）で推定する。

## 会話の引き継ぎ

- Claude / Codex / pi では、エンジンの会話 ID（再開用トークン）が PC ローカルに保存され、通常は新しい発言だけを送って会話を続ける。OpenAI 互換は毎回、履歴全体を送る。
- 次の場合は会話履歴の本文を全部送り直す（ツールの実行結果は含まれない）: エンジンを変えた / 別の PC で続けた / ✎ 編集・⑂ 分岐した / エラーの後 / そのチャットの最後の応答から 90 日以上たった。
- 全体設定（バックエンド/モデル設定…）での同じエンジン内のモデル変更、ペルソナの変更、**停止**（英語表示では Stop）では会話が維持される。

## 使用量の表示

応答が終わるたびにチャット下部へ `🪙 in … · out … · ctx …/… (…%) · $… (total $…)` を表示する。

| エンジン | 表示 |
|---|---|
| Claude | 入出力トークン・コンテキスト使用率・Claude Code が報告する参考値（`$`） |
| Codex | 入出力トークン・コンテキストのみ |
| pi / OpenAI 互換 / モック | 表示なし |

`total` はそのチャットの累計で、設定の適用や再起動でリセットされる。

## 停止と同時実行

- 1 ターンの時間制限は無い。止めるには **停止**（英語表示では **Stop**。途中までの応答は残る）。
- 複数のチャットで同時に生成できる。それぞれ別プロセスで動く。

## AIペルソナ（応答スタイル）

**設定 → AIペルソナ…** で、エージェントの口調・文体を選ぶ。

- 既定は「無し（お客様対応窓口）」（標準の丁寧な応対）。サンプルとして 2 つのペルソナが入っている。**追加…** / **削除** で編集できる。本文の変更は、ペルソナの切り替え・追加・削除・**適用** の時点で保存される（キャンセルしても、それまでに保存された分は残る）。名前の変更はできない。
- チャットタブを右クリック → **このチャットのペルソナ…** で、チャットごとに上書きできる。
- 変わるのは口調だけで、運用ルールが常に優先される。
- 定義は PC ローカル（`data/llm_state/personas.json`）で、他の PC には同期されない。定義が無い PC では「無し（お客様対応窓口）」として扱われる。

## プロバイダ既定のシステムプロンプト

**設定 → プロバイダ既定のシステムプロンプト**（チェック式・Claude のみ）。ON（既定）は Claude Code 標準のシステムプロンプトに MyAnalysis の指示を追記する。OFF は MyAnalysis の指示だけにする。全チャットの次の送信から反映される（生成中の応答は元の設定のまま完了する）。このメニューで一度切り替えると、その PC では `config.toml` の `use_provider_system_prompt` より優先される。

## エンジン別の補足

- **Claude**: 実行ファイルは `[claude_code].bin` → 環境変数 `CLAUDE_CODE_BIN` → エディタ拡張の同梱版 → PATH 上の `claude` の順に探す。ログイン情報・設定は `~/.claude` を Claude Code と共有する。
- **Codex**: ツールの実行は逐次表示されるが、応答の本文は一度にまとめて表示される。
- **pi**: プロバイダの候補は `openai-codex` / `github-copilot` / `llama.cpp`（ローカル）。`thinking` / `effort` は使わない。Windows では bash（Git Bash 推奨）が無いとツール実行が失敗する。図を見て判断させるには、画像入力対応のモデルが必要。
- **OpenAI 互換 HTTP**（Ollama / LM Studio / llama.cpp server 等）:
  - `.env` に `OPENAI_BASE_URL`（`/v1` まで含める。例 `http://localhost:11434/v1`）、必要なら `OPENAI_API_KEY`。モデル名は `models.toml` の `[openai-compat].model`（無ければ `OPENAI_MODEL`）。
  - 毎回 GUI 操作用のツール定義を付けてストリーミングで送るので、**function calling に対応したサーバーとモデルが必要**。小さいモデルではツールをうまく使えない。
  - 通信のタイムアウトは 30 秒（応答の途中で 30 秒以上止まった場合も含む）。モデルの読み込みが遅いと失敗する。
  - このエンジンでは Python の実行・データの読み込み・図の保存はできない。ローカル LLM で解析まで行いたい場合は、pi + `llama.cpp` を使う。
- **モック**: 入力に関係なく定型文を返す動作確認用。応答のたびに stooq.com へ接続する。

## 設定ファイル（手動設定）

GUI で設定すれば手で書く必要はない。手で書く場合は、雛形をコピーして編集する。

| ファイル | 雛形 | 内容 |
|---|---|---|
| `llm_backend/config.toml` | [llm_backend/config.example.toml](../llm_backend/config.example.toml) | エンジンの選択（`[backend].name`）と運用設定（`bin`, `cwd`, `permission_mode`, `allowed_tools`, `sandbox_mode`, `tools` 等） |
| `models.toml` | [models.example.toml](../models.example.toml) | モデル設定（`model` / `thinking` / `effort` / `provider`） |
| `.env` | [.env.example](../.env.example) | 秘密情報と環境変数（`OPENAI_*`, `LLM_BACKEND`, R2 同期、ミーティング共有、同期ドライブ用の設定） |

- **エンジンの選択順**: 環境変数 `LLM_BACKEND` → `config.toml` の `[backend].name` → `OPENAI_BASE_URL`（`mock` ならモック、それ以外は OpenAI 互換）。どれも無ければ OpenAI 互換（api.openai.com）になる。
- `[backend].name`（と環境変数 `LLM_BACKEND`）に書けるのは `claude` / `codex` / `pi` / `openai` / `mock` だけ。それ以外の値だと OpenAI 互換（`OPENAI_BASE_URL=mock` ならモック）で起動し、チャットの先頭にエラーの行が出る（`claude_code` や `openai-compat` はセクション名で、エンジン名ではない）。値を直すか、**設定 → バックエンド/モデル設定…** で選び直す（`.env` の `LLM_BACKEND` は GUI の選択より優先されるので、そこが原因なら `.env` を直す）。
- `.env` に `LLM_BACKEND` を書くと、起動のたびに GUI での選択より優先される。
- `models.toml` の Claude 用の値: `thinking` = `enabled` / `adaptive` / `disabled`。`effort` = `low` / `medium` / `high` / `xhigh` / `max` / `ultracode`。Codex の `effort` = `minimal` / `low` / `medium` / `high`。
- 手で編集したら、再起動するか GUI で **適用** し直す（起動時に一度だけ読む）。ただし `[backend].name` / `model` / `provider` を手で変えた場合は再起動する（ダイアログは古い値を表示するので、そのまま適用すると元に戻る）。
- TOML の構文が壊れていると黙って無視される。`config.toml` なら既定のエンジン（OpenAI 互換）に、`models.toml` ならエンジンの既定モデルに戻る。**適用** では壊れた `config.toml`（モック以外を選んだときは `models.toml` も）が、＋ / － では `models.toml` だけが `.bak` に退避して作り直される。**作り直したファイルには GUI が書くキーしか残らない**ので、手で書いた設定（`permission_mode` 等）は `.bak` から戻す。
- [R2 設定同期](config_sync_ja.md#複数-pc-での設定同期) を使っていると、`config.toml` と `models.toml` の変更は他の PC にも配られる。
- Windows のパスは `"C:/Users/..."` の形で書く。GUI が書き換えるキー（`name`, `bin`, `model`, `provider`）に `'...'`（リテラル文字列）を使うと、GUI の **適用** が失敗する。
- `.env` の注意:
  - 読むのはリポジトリ直下の `.env` だけで、起動時に一度だけ読む。シェルの環境変数の方が優先される。
  - **行末コメントは使えない**（`KEY=value # メモ` は値が `value # メモ` になる）。
  - 前後の引用符は外される。`export ` 付きの行も読める。
  - `.env` の値はエージェントのプロセスにも渡る（→ [セキュリティ](../README_ja.md#セキュリティ)）。
