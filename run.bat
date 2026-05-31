@echo off
rem MyAnalysis GUI 起動用ランチャー
setlocal

rem このバッチがあるディレクトリへ移動(ダブルクリック起動でも確実に動くように)
cd /d "%~dp0"

rem .venv があれば有効化、無ければグローバル python を使う
if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
)

python tool.py
set EXITCODE=%ERRORLEVEL%

rem エラー時は画面を残して原因が見えるようにする
if not "%EXITCODE%"=="0" (
    echo.
    echo [run.bat] tool.py が終了コード %EXITCODE% で終了しました。
    pause
)

endlocal
exit /b %EXITCODE%
