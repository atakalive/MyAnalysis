@echo off
rem MyAnalysis GUI launcher (windowless — pythonw で常駐コンソールを出さない)
setlocal

rem Move to this script's directory so double-click launch works reliably
cd /d "%~dp0"

rem PATH 等の派生環境を子に渡すため activate は残す（tunnel/claude 等が PATH 参照）
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"

rem venv の pythonw を優先、無ければ PATH 上の pythonw
set "PYW=pythonw"
if exist ".venv\Scripts\pythonw.exe" set "PYW=.venv\Scripts\pythonw.exe"

rem detached 起動 → この cmd 窓は即閉じる（GUI はコンソール無しで残る）。
rem "" は start のタイトル引数（実行ファイルパスを引用符で囲むため必須）。
rem エラーやコンソール出力を見たいときは `python tool.py` を直接実行する。
start "" "%PYW%" tool.py

endlocal
