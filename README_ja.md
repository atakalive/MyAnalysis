# MyAnalysis

[English](README.md) | [日本語](README_ja.md)

計測データを、AI エージェントとのチャットで解析するデスクトップアプリ（Python / PySide6）。

「A と B の関係を図にして」と頼むと、エージェントがデータの構成を調べ、Python で解析・作図し、図をタブに表示する。チャット履歴と、エージェントが保存した解析コード・図・レポートはデータセットのフォルダに置かれるので、同期ドライブに置けば別の PC で続きから再開できる。

**リポジトリ:**
- **GitHub（安定版）:** <https://github.com/atakalive/MyAnalysis>
- **GitLab（開発版）:** <https://gitlab.com/atakalive/MyAnalysis>

---

## QuickStart

**前提**: Windows 11 / Python 3.11 以上（動作確認は 3.12。python.org 版）/ Git for Windows（clone と更新、Claude Code の Bash ツールに使う）/ VS Code に Claude Code 拡張を入れてサインイン済み。他のエンジンを使う場合 → [AI エンジン](#ai-エンジン)

> [!WARNING]
> エージェントは、あなたのユーザー権限で、確認なしにコマンドを実行する。依頼内容と読んだデータはエンジンの提供元に送られる。→ [セキュリティ](#セキュリティ)

1. **インストール**: コマンドプロンプト（Win+R → `cmd`）で実行する。置き場所は OneDrive などの同期フォルダ以外。

   ```bat
   cd /d %USERPROFILE%
   git clone https://github.com/atakalive/MyAnalysis.git
   cd MyAnalysis
   py -3 -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   ```

   `py` が無い（Microsoft Store 版の Python）ときは `python -m venv .venv`。

2. **起動**: `MyAnalysis` フォルダの `run.bat` をダブルクリックする（venv の有効化は不要）。UI は英語で起動するので、先に **Settings → Language / 言語 → 日本語** に切り替える。以降の手順は日本語表示のメニュー名で書く。ウィンドウが出なければ → [トラブルシューティング](#トラブルシューティング)
3. **AI エンジン**: **設定 → バックエンド/モデル設定…** を開き、「利用方法（エンジン）」で **Claude（VS Code 同梱エンジン）** を選ぶ。モデル欄は空のままでよい。**疎通確認** を押して `OK（…秒 / <モデル名>）` と出たら **適用**。失敗と出たら、VS Code の Claude Code 拡張にサインインしているか確かめる。[R2 設定同期](docs/config_sync_ja.md) を使う 2 台目以降の PC では、先に [2 台目以降](docs/config_sync_ja.md#2-台目以降) を読む。
4. **データセット**: **ファイル → データセットを新規登録** で計測データのフォルダを選ぶ。名前にはフォルダ名が入るので、記号入り・`-` 始まりなら変える（→ [データセット](#データセット)）。登録が済むと、そのデータセットが開く。
5. **依頼**: 右のチャット欄に「このデータセットの中身を説明して」「`<列A>` と `<列B>` の関係を図にして」などと書いて **Ctrl+Enter**。応答には数十秒〜数分かかることがあり、図は左側のタブに開く。その場で書いたコードは残らないので、再現に使うなら「コードも保存して」と頼む（→ [使い方](#使い方)）。`[エラー: …]`（英語表示では `[error: …]`）が出たら → [トラブルシューティング](#トラブルシューティング)
6. **保存**: タブ構成とチャットは自動保存されない。こまめに **ファイル → セッションを保存** し、終わったら **ファイル → 保存して終了**（→ [保存と再開](#保存と再開)）。

---

## 目次

- [インストールと起動](#インストールと起動)
- [AI エンジン](#ai-エンジン)
- [データセット](#データセット)
- [使い方](#使い方)
- [保存と再開](#保存と再開)
- [セキュリティ](#セキュリティ)
- [オプション機能](#オプション機能)
- [トラブルシューティング](#トラブルシューティング)
- [アンインストール](#アンインストール)
- [制限事項](#制限事項)
- [開発者向け情報](#開発者向け情報)
- [ライセンス](#ライセンス)

---

## インストールと起動

- venv の名前は `.venv` にする。`run.bat` はこの名前だけを探し、自分で有効化する。
- `python` で始まるコマンド（CLI など）は、コマンドプロンプトを開くたびに `MyAnalysis` フォルダで `.venv\Scripts\activate` を実行してから打つ。
- 更新するときは、アプリを閉じて `git pull` → `.venv\Scripts\python -m pip install -r requirements.txt` → 起動。データセットには影響しない。

- PowerShell で `activate` が失敗するときは、`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` を実行するか、コマンドプロンプトを使う。
- TIFF 画像を開くときは `.venv\Scripts\python -m pip install tifffile` も入れる。
- パッケージの版は固定していない。動かないときは動作確認済みの版に合わせる（Python 3.12.1 / PySide6 6.10.1 / pyqtgraph 0.14.0 / numpy 1.26.4 / pandas 2.2.0 / matplotlib 3.8.2 / Pillow 10.3.0。これらは Python 3.12 向け）。
- データなしで画面を試すときは `python tool.py --demo`。
- macOS / Linux で使うときは `python3 -m venv .venv` → `.venv/bin/python -m pip install -r requirements.txt` → `.venv/bin/python tool.py`（動作は未検証）。

---

## AI エンジン

設定は [QuickStart](#quickstart) の手順 3。

- 推奨は Claude（動作確認が最も多い）。「Claude（VS Code 同梱エンジン）」は、VS Code の拡張が見つからなければ PATH 上の `claude` コマンドを使う。
- Claude Code / Codex CLI / pi は、それぞれのエンジンにログインして使う前提（各サービスのサブスクリプション、または pi のローカル llama.cpp）。API キーでの従量課金利用は想定しておらず、動作確認もしていない。アプリに API キーの設定欄は無い（OpenAI 互換 HTTP だけは `.env` の `OPENAI_API_KEY` を使う）。
- 解析（Python の実行）ができるのは Claude Code / Codex CLI / pi。OpenAI 互換 HTTP は、タブを開くなどの GUI 操作しかできない。
- **モデル欄に入れるもの**
  - Claude・Codex: 空でよい（そのエンジンの既定モデル）。Claude で指定するなら `opus` / `sonnet` / `haiku` などのエイリアス。Claude では **疎通確認** の結果 `OK（…秒 / <モデル名>）` に正式なモデル ID が出るので、それをそのまま使える。Claude・Codex ともダイアログに候補があり、**設定 → バックエンドの状況…** の **モデル取得** で、エンジンが知っている新しい ID を候補に追加できる。
  - pi: **プロバイダ**（`openai-codex` / `github-copilot` / `llama.cpp`）を選び、モデルは `pi --list-models` に出る ID（ダイアログに候補あり。**モデル取得** でも追加できる）。
  - OpenAI 互換 HTTP: サーバー側のモデル名を入れる（Ollama なら `ollama list` に出る名前）。空だと `.env` の `OPENAI_MODEL`、それも無ければ `gpt-4o-mini` になり、Ollama などでは失敗する。
- effort（考える量）は **設定 → バックエンド/モデル設定…** で選べる（Claude・Codex・pi。チャットごとにも変えられる）。通常は「既定」のままでよい（→ [docs/engines_ja.md](docs/engines_ja.md#モデルの指定)）。

Claude 以外のエンジン（Codex CLI / pi / Ollama などの OpenAI 互換 HTTP）を使うとき、チャットごとにモデルを変えるとき、設定ファイルを手で書くときは [docs/engines_ja.md](docs/engines_ja.md) を読む。

---

## データセット

データセットは「計測データを入れたフォルダ 1 つ」と「その名前」の組。登録は PC ごと。

- **名前**: 空白・`/`・`\`・先頭の `.` は登録できない。**記号（`"` `'` `&` `$`）を含む名前、`-` で始まる名前は避ける。** 登録はでき GUI からは開けるが、エージェントや CLI のコマンドでオプションと解釈されたり引用が壊れたりして、指定できなくなる。
- **データの形式**: 構成は自由（エージェントが読む前に調べる）。どのサブフォルダにも同じファイル名の CSV（例: `data.csv`）が 1 つずつある構成は、標準の読み込み関数でそのまま読める。
- **アプリがフォルダに作るもの**: `myanalysis.toml`（設定）、`meta.json`（一覧表示用）、`analyses/`（解析モジュール）、`_work/`（出力・チャット履歴・タブ構成）。フォルダへの書き込み権限が要る。アプリ本体は計測ファイルを書き換えない。エージェントには書き換えないよう指示しているが、機械的な制限ではない。
- **同期ドライブに置いて複数の PC で使う場合**: 各 PC で同じ名前で登録する。**同じデータセットを 2 台で同時に開かない**（PC 間の排他制御は無く、タブ構成と、両方で変更したチャットは後から保存した方が勝つ）。別の PC で最初に送信すると、チャットの全履歴を送り直す。

登録を消すときは、**ファイル → データセットを開く…** で選んで **登録を削除** を押す（この PC の登録簿から全ホスト分のパスごと消える。R2 同期を使っていれば他の PC の登録も消える。フォルダのファイルは消えない）。他の PC の登録が届いていて「このホストにパス無し」と出る行は、同じ一覧の **この PC のパスを登録…** でこの PC のフォルダを選べば開けるようになる。CLI で登録するとき、出力先や読み込み形式（`myanalysis.toml`）を変えるときは [docs/usage_ja.md](docs/usage_ja.md#データセット) を読む。

---

## 使い方

**エージェントの成果物（図・コード・レポート）は、すべてデータセットの `_work/`（`figures/`・`code/` など）に保存される。** 表示される図も、ふつうはここに保存されたファイル。

- エージェントは `.venv` の Python を使い、足りないパッケージ（scipy など）を `.venv` に pip install することがある。
- チャットタブの右クリック → **閉じる** は、チャットの**削除**。残したいなら **アーカイブ**。
- **Ctrl+F**（**表示 → チャットを検索…**）で過去のチャットを検索し、結果を新しいチャットタブに出す。**AI 検索** にすると、エージェントがチャットを探して結論をまとめ、そのまま続きを聞ける（→ [docs/usage_ja.md](docs/usage_ja.md#チャット)）。

顕微鏡画像などを ImageJ のように見るとき（16bit・多次元 TIFF、LUT、チャンネル合成）は、画像ビューアを使う。チャットで「`<ファイルのパス>` を画像ビューアで開いて」と頼むか（エージェントは頼まれたときだけ画像ビューアを開く）、CLI で開く。手順は [docs/usage_ja.md](docs/usage_ja.md#図ビューアと画像ビューア) を読む。

---

## 保存と再開

- **タブ構成とチャットは自動保存されない。** **ファイル → セッションを保存** か **保存して終了** で保存する。クラッシュや強制終了では、最後の保存以降のチャットが消える。
- 未保存のまま閉じると確認が出る。ただし、どのデータセットにも紐づかなかったチャットは保存されず、確認も出ない（紐づく条件は [docs/usage_ja.md](docs/usage_ja.md#チャット)）。
- 再開するには **ファイル → データセットを開く…**（そのデータセットのタブとチャットを復元する）か、**ファイル → 前回のセッションを復元**（最後に保存したときに開いていたデータセットをまとめて開く）。

何がどのファイルに保存されるかは [docs/usage_ja.md](docs/usage_ja.md#保存データと再開)。

---

## セキュリティ

- **エージェントは、あなたのユーザー権限で、確認なしに動く。** 既定では Claude は `bypassPermissions`、Codex はサンドボックス無し、pi は全ツールが許可されている。
- **依頼内容と、エージェントが読んだデータは、エンジンの提供元に送られる。** エージェントはシェルや Web 検索などで外部とも通信する。MyAnalysis 本体はテレメトリを送らない。
- `.env` とシェルの環境変数（API キーなど）は、エージェントから読める。

エージェントの権限を絞るときは [docs/security_ja.md](docs/security_ja.md) を読む（絞ると GUI 操作や解析が動かなくなる）。

---

## オプション機能

| 機能 | 内容 | 資料 |
|---|---|---|
| 自作の解析モジュール | Python 1 ファイルで対話的な解析タブを作る。GUI なしで PNG を一括出力する | [docs/analysis_module_ja.md](docs/analysis_module_ja.md) |
| CLI | GUI の操作・データセットの登録・点検をコマンドで行う | [docs/cli_ja.md](docs/cli_ja.md) |
| 複数 PC での設定同期 | 登録簿とエンジンの設定を、Cloudflare R2 で PC 間で同期する | [docs/config_sync_ja.md](docs/config_sync_ja.md) |
| ミーティング共有 | 解析画面とチャットを、離れた相手のブラウザへ共有する（外部の相手とつなぐには Cloudflare の設定が必要）。**参加リンクを持つ人は、承認なしにエージェントを動かせる** | [docs/meeting_share_ja.md](docs/meeting_share_ja.md) |

---

## トラブルシューティング

| 症状 | 対処 |
|---|---|
| `run.bat` でウィンドウが出ない | 原因を示すダイアログが出る。Python が古い場合はダイアログだけ（ログには残らない）。それ以外は `data/logs/gui-crash-*.log` に traceback が残る（warning 以上のログは `data/logs/myanalysis.log`）。「必要なパッケージがありません」と出たら `MyAnalysis` フォルダで `.venv\Scripts\python -m pip install -r requirements.txt`（`.venv` が無ければ QuickStart の手順 1）。Windows の「pythonw が見つからない」エラーも `.venv` が無いとき。詳しく見るには venv を有効にして `python tool.py` |
| チャットに「AI エンジンが選ばれていないため送信できません」と出る | AI エンジンが設定されていない → [QuickStart](#quickstart) の手順 3 |
| `[エラー: RuntimeError('Claude Code engine not found…')]` | Claude Code の拡張を入れてサインインする（または `llm_backend/config.toml` の `[claude_code].bin` か環境変数 `CLAUDE_CODE_BIN` で `claude` の場所を指定する） |
| チャットに `[エラー: …]` と出る | 認証切れ・レート制限など。再送するか、エンジンにログインし直す |
| 「保存に失敗しました（検証NG）」 | 同期ドライブの不調。rclone などのマウントで起きやすい → [docs/troubleshooting_ja.md](docs/troubleshooting_ja.md)（点検・修復コマンド `doctor` を含む） |

その他の症状、同期ドライブで起きる問題、不具合を報告するときに添えるもの → [docs/troubleshooting_ja.md](docs/troubleshooting_ja.md)

---

## アンインストール

1. アプリのフォルダ（リポジトリ）と `%USERPROFILE%\.myanalysis`（macOS / Linux は `~/.myanalysis`）を削除する。
2. データセットのフォルダを元に戻すなら、`myanalysis.toml`・`meta.json*`・`analyses/`・`_work/` を削除する。`analyses/` には解析モジュールが、`_work/` にはチャット履歴と出力がある。

エンジン側の会話記録や、R2・Cloudflare の設定まで消すときは [docs/usage_ja.md](docs/usage_ja.md#アンインストールの詳細) を読む。

---

## 制限事項

- 動作確認は Windows 11 だけ。
- 解析タブは、開いている間データをメモリに置き続ける。大きなデータを多数開くとメモリを圧迫する。
- 一部の表示は英語のまま（→ [表示言語](docs/usage_ja.md#表示言語)）。
- 詳細 → [docs/usage_ja.md](docs/usage_ja.md#制限事項の詳細)

---

## 開発者向け情報

MyAnalysis 本体の開発（構成・テスト・ホットリロード・規約）は [docs/development_ja.md](docs/development_ja.md)。AI コーディングエージェント向けの規約は [CLAUDE.md](CLAUDE.md)。

---

## ライセンス

MIT License（[LICENSE](LICENSE)）
