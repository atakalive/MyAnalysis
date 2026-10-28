#!/bin/sh
# MyAnalysis GUI launcher for macOS / Linux（run.bat 相当）。ターミナルの前面で動かす:
# 起動の失敗はダイアログにならず stderr にだけ出る（common/crashlog.py の show_fatal）。
cd "$(dirname "$0")" || exit 1

# venv の python を優先、無ければ PATH 上の python3
PY=python3
if [ -x .venv/bin/python ]; then
    PY=.venv/bin/python
    # run.bat の activate と同じく、子（claude・cloudflared など）に venv の PATH を渡す
    PATH="$PWD/.venv/bin:$PATH"
    VIRTUAL_ENV="$PWD/.venv"
    export PATH VIRTUAL_ENV
fi

# バイトコードをリポジトリ内のローカルディスクへ退避する (Issue #96)。
# 同期マウント上の analysis.py を import すると CPython が __pycache__/*.pyc を
# tmp+rename で書き、マウントによっては 0 バイト化する（run.bat と同じ）。
: "${PYTHONPYCACHEPREFIX:=$PWD/data/pycache}"
export PYTHONPYCACHEPREFIX

exec "$PY" tool.py "$@"
