# MyAnalysis

[English](README.md) | [日本語](README_ja.md)

計測データを、AI エージェントとのチャットで解析するデスクトップアプリ（Python / PySide6）。

「A と B の関係を図にして」と頼むと、エージェントがデータの構成を調べ、Python で解析・作図し、図をタブに表示する。チャット履歴と、エージェントが保存した解析コード・図・レポートはデータセットのフォルダに置かれるので、同期ドライブに置けば別の PC で続きから再開できる。

**リポジトリ:**
- **GitHub（安定版）:** <https://github.com/atakalive/MyAnalysis>
- **GitLab（開発版）:** <https://gitlab.com/atakalive/MyAnalysis>

---

## QuickStart

**対応 OS**: Windows 11 x64、macOS 14.4 以降（Intel / Apple Silicon）、Ubuntu 24.04 以降・Debian 12 以降（amd64）。macOS の実画面試験は未実施。

**AI エンジン**: 選択するサービスのアカウントと認証が必要。Claude Code は有料プランでログインする。未導入のエンジンはアプリの **設定 → バックエンドの状況…** から導入できる（→ [AI エンジン](#ai-エンジン)）。

> [!WARNING]
> エージェントは、あなたのユーザー権限で、確認なしにコマンドを実行する。依頼内容と読んだデータはエンジンの提供元に送られる。→ [セキュリティ](#セキュリティ)

1. **インストール**: [Releases](https://github.com/atakalive/MyAnalysis/releases/latest) から自分の OS のファイルを取得して開く。

   | OS | ファイルと開き方 |
   |---|---|
   | Windows | `setup.bat` をダブルクリック |
   | macOS | `MyAnalysis.pkg` を開いて導入後、アプリケーションの **MyAnalysis** を開く |
   | Ubuntu / Debian | `MyAnalysis.deb` をソフトウェアのインストール画面で導入後、アプリメニューの **MyAnalysis** を開く |

   初回は端末に進捗が表示され、既存の Python・Git・本体・パッケージは再利用し、不足分だけを導入する。Windows の Python は python.org 版を使い、Microsoft Store 版は使わない。OS の導入画面が出たときは操作する。`.pkg`・`.deb` の導入と、通常ユーザーでの初回準備は2段階。

   macOS の配布物は未署名。拒否された場合は **システム設定 → プライバシーとセキュリティ → このまま開く** でこのファイルだけを許可する。Windows の SAC や組織のポリシーで拒否された場合は、表示された理由を確認し、[詳しい導入手順](#詳しい導入手順)を使う。OS の保護機能は自動変更しない。

2. **起動**: セットアップ完了後に画面が開く。次からは Windows のスタートメニュー、macOS のアプリケーション、Linux のアプリメニューの **MyAnalysis** を使う。macOS / Linux では端末を閉じずに使う。UI は英語で起動するので、**Settings → Language / 言語 → 日本語** に切り替える。画面が出なければ [トラブルシューティング](#トラブルシューティング)。

3. **AI エンジン**: **設定 → バックエンド/モデル設定…** を開き、「利用方法（エンジン）」で **Claude（VS Code 同梱エンジン）** を選ぶ。モデル欄は空のままでよい。**疎通確認** を押して `OK（…秒 / <モデル名>）` と出たら **適用**。失敗と出たら、VS Code の Claude Code 拡張（または `claude` コマンド）に、有料プランのアカウントでサインインしているか確かめる。[R2 設定同期](docs/config_sync_ja.md) を使う 2 台目以降の PC では、先に [2 台目以降](docs/config_sync_ja.md#2-台目以降) を読む。
4. **データセット**: **ファイル → データセットを新規登録** で計測データのフォルダを選ぶ。名前にはフォルダ名が入るので、記号入り・`-` 始まりなら変える（→ [データセット](#データセット)）。登録が済むと、そのデータセットが開く。
5. **依頼**: 右のチャット欄に「このデータセットの中身を説明して」「`<列A>` と `<列B>` の関係を図にして」などと書いて **Ctrl+Enter**（macOS は **⌘+Return**）。応答には数十秒〜数分かかることがあり、図は左側のタブに開く。`[エラー: …]`（英語表示では `[error: …]`）が出たら → [トラブルシューティング](#トラブルシューティング)
6. **保存**: タブ構成とチャットは自動保存されない。こまめに **ファイル → セッションを保存** し、終わったら **ファイル → 保存して終了**（→ [保存と再開](#保存と再開)）。

---

## 目次

- [インストールと起動](#インストールと起動)
- [詳しい導入手順](#詳しい導入手順)
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

- venv の名前は `.venv` にする。`run.bat`（macOS / Linux は `run.sh`）はこの名前だけを探し、自分で有効化する。
- `python` で始まるコマンド（CLI など）は、コマンドプロンプトを開くたびに `MyAnalysis` フォルダで `.venv\Scripts\activate`（macOS / Linux はターミナルで `source .venv/bin/activate`）を実行してから打つ。
- 更新するときは **ヘルプ → MyAnalysis を更新** を押す。GitHub の `main` を取得して進め（fast-forward のみ。手元に GitHub に無いコミットや、更新で上書きされる変更があれば何も変えない）、`requirements.txt` が変わっていれば `pip install -r requirements.txt` も実行してから、再起動するかを訊く。再起動すると開いていたデータセットを開き直す。押したときだけ動き、起動時の確認や「更新があります」の通知はしない。データセットには影響しない。
- GitHub の **Download ZIP** で入れたものは起動できるが、`.git` が無いので **ヘルプ → MyAnalysis を更新** が使えない。`git clone` で入れる。
- 手で更新するときは、アプリを閉じて `git pull` → `.venv\Scripts\python -m pip install -r requirements.txt`（macOS / Linux は `.venv/bin/python`）→ 起動。

- PowerShell で `activate` が失敗するときは、コマンドプロンプトを使うか、`.venv\Scripts\python` を直接実行する。
- TIFF 画像を開くときは `.venv\Scripts\python -m pip install tifffile`（macOS / Linux は `.venv/bin/python`）も入れる。
- パッケージの版は固定していない。動かないときは動作確認済みの版に合わせる（Python 3.12.1 / PySide6 6.10.1 / pyqtgraph 0.14.0 / numpy 1.26.4 / pandas 2.2.0 / matplotlib 3.8.2 / Pillow 10.3.0。これらは Python 3.12 向け）。
- データなしで画面を試すときは `python tool.py --demo`。

### macOS / Linux

GUI の OS 別実機試験の結果は [検証表](docs/installer-validation.md) を参照。端末での導入は [詳しい導入手順](#詳しい導入手順)。

- **起動**: `./run.sh` で起動する。`run.sh` は `.venv/bin/python` を使い、バイトコード（`__pycache__`）をリポジトリの `data/pycache` に置く。`.venv/bin/python tool.py` を直接実行すると、同期フォルダ上の解析を開いたときにそのフォルダへ `__pycache__` を作る。
- **ターミナル**: アプリはターミナルの中で動くので、使っている間（更新や再起動の後も）ターミナルを閉じない。起動に失敗した理由は、ダイアログではなくターミナルに出る。
- **キー**: この README と docs の Ctrl は ⌘、Alt は ⌥ と読み替える（送信は ⌘+Return、チャットの検索は ⌘F、前後の解析タブは ⌘←/→、前後のデータセットは ⌥←/→）。文字入力欄の中では、⌘←/→（行頭・行末）と ⌥←/→（単語単位の移動）は入力欄が使う。
- **メニュー**: 「終了」（英語表示では「About MyAnalysis…」も）は、メニューバー左端のアプリのメニュー（「Python」）に入る。ほかのメニューはこの README のとおり。
- **ファイルのアクセス**: 書類・デスクトップ・ダウンロードのフォルダや、外付け・ネットワークのボリュームのデータを初めて開くとき、macOS がターミナルにアクセスを許可するかを訊くので、許可する。拒否したときは、システム設定 → プライバシーとセキュリティ → ファイルとフォルダ で「ターミナル」に許可する。
- **証明書**: python.org 版の Python では、アプリケーション → Python 3.x の **Install Certificates.command** を一度実行する。実行しないと、HTTPS の OpenAI 互換エンジンが `CERTIFICATE_VERIFY_FAILED` で失敗する（Claude・Codex・pi には関係しない）。Homebrew 版では要らない。
- **同期ドライブ**: FUSE（macFUSE・FUSE-T・rclone）とネットワーク（SMB・NFS・AFP・WebDAV）のマウントは同期ドライブ（fragile）として扱い、ほか（APFS などのディスク、iCloud・Google ドライブ・Dropbox・OneDrive のフォルダ）は通常のディスクとして扱う（→ [docs/troubleshooting_ja.md](docs/troubleshooting_ja.md#同期ドライブでの書き込み)）。
- **Claude サブ**: トークンはログインキーチェーン（項目名 `MyAnalysis-claude-sub`）に置く。保存するとき・読むときに、キーチェーンの確認が出ることがある。SSH などキーチェーンを使えないセッションでは「未設定」に見える。
- **Linux**: Debian / Ubuntu では、先に `sudo apt install git python3-venv libxcb-cursor0` を入れておく。手順は macOS と同じ（`xcode-select`・`scutil`・証明書・ファイルのアクセスは macOS だけのもの）。

---

## AI エンジン

初回の設定は [QuickStart](#quickstart) の AI エンジンの項目。

- 推奨は Claude（動作確認が最も多い）。「Claude（VS Code 同梱エンジン）」は、VS Code の拡張が見つからなければ PATH 上の `claude` コマンドを使う。
- 未導入の Claude・Codex・pi は **設定 → バックエンドの状況… → インストール** で入れられる。Node.js / npm の準備は不要。公式配布物を `~/.myanalysis/backends` に配置し、pi 用の実行環境も自動で用意する。再起動や PATH 設定なしで認識されるので、導入後は **ログイン** へ進む。Claude は「Claude（VS Code 同梱エンジン）」のままでも、拡張がなければアプリ管理版を使う。
- Claude サブ（2 つ目の Claude のアカウント）も選べる。`claude setup-token` で作ったトークンを設定ダイアログに貼る（→ [docs/engines_ja.md](docs/engines_ja.md)）。
- Codex 拡張を導入してログイン済みなら「Codex（VS Code 拡張 / CLI）」を選ぶだけで使える。アプリ管理版があればそれを使い、なければ PATH、拡張同梱版の順に自動検出する。独自の実行ファイル指定は優先する。
- Claude Code / Codex / pi は、それぞれのエンジンにログインして使う前提（各サービスのサブスクリプション、または pi のローカル llama.cpp）。API キーでの従量課金利用は想定しておらず、動作確認もしていない。アプリに API キーの設定欄は無い（OpenAI 互換 HTTP だけは `.env` の `OPENAI_API_KEY` を使う）。
- 解析（Python の実行）ができるのは Claude Code / Codex / pi。OpenAI 互換 HTTP は、タブを開くなどの GUI 操作しかできない。
- **モデル欄に入れるもの**
  - Claude・Codex: 空でよい（そのエンジンの既定モデル）。Claude で指定するなら `opus` / `sonnet` / `haiku` などのエイリアス。Claude では **疎通確認** の結果 `OK（…秒 / <モデル名>）` に正式なモデル ID が出るので、それをそのまま使える。Claude・Codex ともダイアログに候補があり、**設定 → バックエンドの状況…** の **モデル取得** で、エンジンが知っている新しい ID を候補に追加できる。
  - pi: **プロバイダ**（`openai-codex` / `github-copilot` / `llama.cpp`）を選び、モデルは `pi --list-models` に出る ID（ダイアログに候補あり。**モデル取得** でも追加できる）。
  - OpenAI 互換 HTTP: サーバー側のモデル名を入れる（Ollama なら `ollama list` に出る名前）。空だと `.env` の `OPENAI_MODEL`、それも無ければ `gpt-4o-mini` になり、Ollama などでは失敗する。
- effort（考える量）は **設定 → バックエンド/モデル設定…** で選べる（Claude・Codex・pi。チャットごとにも変えられる）。通常は「既定」のままでよい（→ [docs/engines_ja.md](docs/engines_ja.md#モデルの指定)）。

Claude 以外のエンジン（Codex / pi / Ollama などの OpenAI 互換 HTTP）を使うとき、チャットごとにモデルを変えるとき、設定ファイルを手で書くときは [docs/engines_ja.md](docs/engines_ja.md) を読む。

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
- **Ctrl+←/→** で前後の解析タブへ、**Alt+←/→** で前後のデータセットへ切り替える（どこにフォーカスがあっても効く。ただし文字入力欄では Ctrl+←/→ は単語単位のカーソル移動になる）。
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
| チャットの関連検索（実験的） | 保存済みのチャットを、言い回しが違っても意味の近さで探す。**Ctrl+F** に埋め込み検索と無料検索（一致＋埋め込み。登録済みの全データセットが対象）が加わり、エージェントと AI 検索も使う。**設定 → チャットの関連検索（実験的）…** で有効にする: `onnxruntime`・`tokenizers` を pip で入れ、事前学習済みの埋め込みモデル（Ruri v3 30m・Apache-2.0）を Hugging Face から取得する（アプリに固定した sha256 と照合）。計算はアプリの中で CPU で行い、API・料金・訓練や微調整は要らない。索引はこの PC だけに置く。既定はオフ | [docs/usage_ja.md](docs/usage_ja.md) |
| チャットの数式描画 | チャットの返答の LaTeX の数式（`$…$` / `$$…$$`）を描く。**設定 → 数式の描画…** から導入する: MathJax（Apache-2.0）と数式フォント 1 つを npm レジストリから取得し（アプリに固定した sha512 と照合）、`~/.myanalysis/mathjax` に置き、QtWebEngine（PySide6 に含まれる）で描く。数式を描いている間は Chromium のプロセスが 1 つ動く。入れていなければ数式は LaTeX のソースで出る | — |

---

## 詳しい導入手順

GUI の導入は上の [QuickStart](#quickstart)。端末からは `setup.bat --install-dir C:\Apps\MyAnalysis --no-pause`、macOS / Linux は `bash setup.sh --install-dir "$HOME/MyAnalysis" --no-pause`。`--no-launch` は準備だけを行う。正常な Python・Git を暗黙に更新しない。本体の更新は **ヘルプ → MyAnalysis を更新**。

既存配置、ホーム直下の旧 `MyAnalysis`、以前選んだ配置を再利用する。複数ある場合は `--install-dir` で指定できる。venv は移動しない。ZIP・別リポジトリ・判定不能なフォルダと既存データは上書きしない。

新規の既定配置は Windows `%LOCALAPPDATA%\Programs\MyAnalysis`、macOS `~/Library/Application Support/MyAnalysis/app`、Linux `${XDG_DATA_HOME:-~/.local/share}/myanalysis/app`。インストーラーは同期フォルダから開けるが、本体はローカルへ配置する。

手動で準備する場合は Python 3.11 以上の通常版・64 bit（Windows は python.org 版）、Git と Windows の Git Bash が必要。Intel Mac の新規環境は Python 3.11～3.13。macOS で Git が無い場合は `xcode-select --install` を実行し、OS の導入完了を待つ。Debian / Ubuntu の OS 依存は自動セットアップで不足分だけ入る。

```bat
cd /d %USERPROFILE%
git clone https://github.com/atakalive/MyAnalysis.git
cd MyAnalysis
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
run.bat
```

macOS / Linux:

```sh
cd ~
git clone https://github.com/atakalive/MyAnalysis.git
cd MyAnalysis
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./run.sh
```

セットアップを再実行すれば不足工程を再検査して再開する。正常な環境では通信・pip・管理者認証を行わない。画面を閉じても進行中の OS インストーラーは終了しないので、その導入が終わってから再実行する。依存の競合や検出タイムアウトを未導入とみなして上書きしない。ログの場所は画面に表示され、Windows `%LOCALAPPDATA%\MyAnalysis\setup`、macOS `~/Library/Application Support/MyAnalysis/setup`、Linux `${XDG_STATE_HOME:-~/.local/state}/myanalysis/setup` に残る。

配布物の固定版・照合情報と検証状況は [検証表](docs/installer-validation.md)。

---

## トラブルシューティング

| 症状 | 対処 |
|---|---|
| `run.bat` でウィンドウが出ない | 原因を示すダイアログが出る。Python が古い場合はダイアログだけ（ログには残らない）。それ以外は `data/logs/gui-crash-*.log` に traceback が残る（warning 以上のログは `data/logs/myanalysis.log`）。「必要なパッケージがありません」と出たら `MyAnalysis` フォルダで `.venv\Scripts\python -m pip install -r requirements.txt`（`.venv` が無ければ [詳しい導入手順](#詳しい導入手順)）。Windows の「pythonw が見つからない」エラーも `.venv` が無いとき。詳しく見るには venv を有効にして `python tool.py` |
| `'py' は、内部コマンドまたは外部コマンド…として認識されていません`、または `py -3` が `Unable to create process` で失敗する | Microsoft Store 版 Python か、ランチャ無しで入れた Python、またはランチャが消した Python を指している（`py -0p` でパスが見える）。python.org 版を入れて（ランチャは既定で入る）[詳しい導入手順](#詳しい導入手順)からやり直す。Store 版はセットアップ対象外。`setup.bat` を再実行すると公式 Python を検出・準備する |
| チャットに「AI エンジンが選ばれていないため送信できません」と出る | AI エンジンが設定されていない → [QuickStart](#quickstart) の手順 3 |
| `[エラー: RuntimeError('Claude Code engine not found…')]` | Claude Code の拡張か `claude` コマンドを入れてサインインする（→ [AI エンジン](#ai-エンジン)）。または `llm_backend/config.toml` の `[claude_code].bin` か環境変数 `CLAUDE_CODE_BIN` で `claude` の場所を指定する |
| チャットに `[エラー: …]` と出る | 認証切れ・レート制限など。再送するか、エンジンにログインし直す |
| `./run.sh` でウィンドウが出ない（macOS / Linux） | 理由はターミナルと `data/logs/gui-crash-*.log` に出る。`bash setup.sh` を再実行すると不足工程を再検査する。手動での修復は [詳しい導入手順](#詳しい導入手順) |
| Linux で `Could not load the Qt platform plugin "xcb"` と出る | `libxcb-cursor0` を入れる（Debian / Ubuntu は `sudo apt install libxcb-cursor0`） |
| macOS で `CERTIFICATE_VERIFY_FAILED` と出る | python.org 版の Python なら Install Certificates.command を実行する（→ [macOS / Linux](#macos--linux)） |
| macOS で、データセットが「このホストにパス無し」になる | `python -m llm_bridge host` で今の名前を確かめ、古い名前の登録は `python -m llm_bridge rename-host <古い名前>` で移す（**ファイル → データセットを開く…** の **この PC のパスを登録…** でも足せる） |
| 「保存に失敗しました（検証NG）」 | 同期ドライブの不調。rclone などのマウントで起きやすい → [docs/troubleshooting_ja.md](docs/troubleshooting_ja.md)（点検・修復コマンド `doctor` を含む） |

その他の症状、同期ドライブで起きる問題、不具合を報告するときに添えるもの → [docs/troubleshooting_ja.md](docs/troubleshooting_ja.md)

---

## アンインストール

1. アプリを終了してランチャーを削除する。Windows はスタートメニューの MyAnalysis、macOS は `/Applications/MyAnalysis.app`、Linux はソフトウェア管理画面で `myanalysis` を削除する。`.deb` を削除してもユーザー領域の本体は残る。
   Linux では初回準備で作成した `${XDG_DATA_HOME:-~/.local/share}/applications/myanalysis.desktop` も削除する。
2. 必要なら本体を削除する。既定は Windows `%LOCALAPPDATA%\Programs\MyAnalysis`、macOS `~/Library/Application Support/MyAnalysis/app`、Linux `${XDG_DATA_HOME:-~/.local/share}/myanalysis/app`。旧配置を再利用した場合はその場所。設定・データ・`~/.myanalysis` は別に残るので、必要なものを保存してから選んで削除する。Python・Git・既存 AI エンジンは自動削除しない。
3. データセットのフォルダを元に戻すなら、`myanalysis.toml`・`meta.json*`・`analyses/`・`_work/` を削除する。`analyses/` には解析モジュールが、`_work/` にはチャット履歴と出力がある。

エンジン側の会話記録や、R2・Cloudflare の設定まで消すときは [docs/usage_ja.md](docs/usage_ja.md#アンインストールの詳細) を読む。

---

## 制限事項

- 動作確認は Windows 11 だけ（検証状況 → [macOS / Linux](#macos--linux)）。
- 解析タブは、開いている間データをメモリに置き続ける。大きなデータを多数開くとメモリを圧迫する。
- 一部の表示は英語のまま（→ [表示言語](docs/usage_ja.md#表示言語)）。
- 詳細 → [docs/usage_ja.md](docs/usage_ja.md#制限事項の詳細)

---

## 開発者向け情報

MyAnalysis 本体の開発（構成・テスト・ホットリロード・規約）は [docs/development_ja.md](docs/development_ja.md)。AI コーディングエージェント向けの規約は [CLAUDE.md](CLAUDE.md)。

---

## ライセンス

MIT License（[LICENSE](LICENSE)）
