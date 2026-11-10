#!/bin/sh
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
    echo 'MyAnalysis environment is missing. Run bash setup.sh to install or repair it.' >&2
    exit 1
fi
: "${PYTHONPYCACHEPREFIX:=$PWD/data/pycache}"
export PYTHONPYCACHEPREFIX
exec .venv/bin/python -m installer.core --launch --install-dir "$PWD" "$@"
