# インストーラーの検証記録

2026-11-11。実装差分を適用した作業ツリー（基点 `7562883`）で検証。配布物の生成、wheel の依存解決、GUI の実行は、それぞれ別の結果として記録する。

## 実行した検証

| 環境 | Python / ABI | 結果 |
|---|---|---|
| Windows 11 x64（build 26200） | python.org 3.12.4 / cp312 | 最終の全体テスト 3,616 成功・24 スキップ。Windows / Linux のソース改行差でも配布物が一致することを追加確認 |
| Windows 11 x64 | 同上 | 日本語・空白・`!` を含む隔離 checkout で、単体 `setup.bat` の既存環境再利用が成功。通信・pip なし、既存登録簿を保持 |
| Windows 11 x64 | 同上 | 隔離環境で GUI を起動し、起動ごとの識別子を渡した Bridge の `list-open-datasets` 応答まで確認（Qt offscreen）。利用者の設定・認証・データは使用しない |
| Ubuntu 24.04 / WSL x86_64 | 3.12.3 / cp312 | 共通処理・更新・任意依存のテスト、`bash -n`、`dpkg-deb` での `.deb` 生成・依存宣言・内容を検証。基盤に ensurepip がないため、実 venv のローカル wheel 補充試験はこの環境ではスキップ |
| macOS Intel / Apple Silicon | cp313 向け | 生成する `.app` の plist・Terminal 起動・同梱コア・ユーザー用 postinstall 不在をテスト。実機の `.pkg` 導入・Gatekeeper・TCC・実画面試験は未実施 |

Windows の実試験は既存環境の再利用とローカル wheel の補充を対象とする。Python・Git がない実機への OS インストーラー実行、再起動要求、SAC 有効機の拒否、Linux デスクトップの認証画面は実機確認済みとはしない。

実行環境の必須依存は PySide6 / Addons / Essentials / shiboken6 6.10.2、pyqtgraph 0.14.0、numpy 1.26.4、pandas 2.2.2、matplotlib 3.9.0、Pillow 12.3.0。下記の新規環境向け wheel 解決結果とは区別する。

ローカルで作成した wheel を使い、不足するパッケージだけが追加され、既存 numpy の版が変わらないことを確認。worker の異常終了では、Windows はジョブが通常コマンドの子を停止し、POSIX は残存プロセスを検出して次の変更を拒否することも検証する。OS 導入処理には停止するジョブを適用しない。

## wheel の解決

Python 3.13 / cp313、Windows x64、macOS 14.4 Intel・arm64、Debian 12 互換の manylinux amd64 を指定し、必須要件と `onnxruntime`・`tokenizers`・`tifffile`・`boto3` を、推移的依存も含めて wheel のみで事前解決した。4対象とも解決成功。全パッケージの版は [解決結果](installer-resolution-20261111.json) に記録する。

| 項目 | Windows x64 | macOS Intel | macOS arm64 | Linux amd64 |
|---|---|---|---|---|
| PySide6 | 6.12.0 | 6.12.0 | 6.12.0 | 6.12.0 |
| numpy / pandas | 2.5.4 / 3.1.1 | 同左 | 同左 | 同左 |
| onnxruntime | 1.31.1 | 1.23.2 | 1.31.1 | 1.31.1 |
| tokenizers | 0.23.3 | 0.23.3 | 0.23.3 | 0.23.3 |
| tifffile / boto3 | 2026.11.8 / 1.43.127 | 同左 | 同左 | 同左 |

これはクロス環境の wheel 解決であり、対象 OS 上での Qt・任意機能の実行結果ではない。任意機能をインストーラーから無条件に導入しない。既存 Python 3.11 以上は再利用し、その実際の ABI で解決する。新規 Intel Mac 環境は 3.11～3.13 に限定する。

## 直接取得する固定配布物

取得先の HTTPS、サイズと SHA-256 を検証し、照合したキャッシュだけを再利用する。照合値の根拠は [Python 公式リリース](https://www.python.org/downloads/release/python-31316/) と [Git for Windows 公式リリース](https://github.com/git-for-windows/git/releases/tag/v2.56.0.windows.4)。

| 配布物 | サイズ（byte） | SHA-256 |
|---|---:|---|
| python-3.13.16-amd64.exe | 29935584 | `fb4f9f5d438b2396da0086dc70b935c530cb578e37adc6d354f7ad2037fee83b` |
| python-3.13.16-macos11.pkg | 70394335 | `30666509020b4da0dd8bc2e773255f34d76b7bb80b66960a928d5f6daa0192d7` |
| Git-2.56.0.4-64-bit.exe | 66850032 | `4961a4a8cc778d8b6e00a5819e5fe4d078b754555aff7aedc2d77a5d74c2de80` |

通常版を使用し、Windows の Store エイリアス、Store 由来のベース環境、32 bit、free-threaded ビルド、embeddable-only 環境は新規 venv のベースにしない。既存の正常な Python・Git の更新は行わない。

## 再現

```sh
python -m installer.build
python -m pytest tests/test_installer.py tests/test_self_update.py tests/test_embed_model.py
python -m installer.build --out dist --deb  # Debian / Ubuntu
python -m installer.build --out dist --pkg  # macOS、未署名
```

OS の実インストール試験は専用環境で行う。テスト中に利用者全体の PATH・レジストリ・Git 設定・認証情報を変更しない。
