#!/bin/bash
# Embedded into the standalone setup.sh. Keep compatible with macOS Bash 3.2.
set -eu
install_dir=''
no_pause=0
no_launch=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --install-dir) [ "$#" -ge 2 ] || { echo '--install-dir requires a path'; exit 2; }; install_dir=$2; shift 2 ;;
        --no-pause) no_pause=1; shift ;;
        --no-launch) no_launch=1; shift ;;
        *) echo "Unknown argument: $1"; exit 2 ;;
    esac
done
fail() { echo "Setup incomplete: $*"; if [ "$no_pause" = 0 ] && [ -t 0 ]; then read -r -p 'Press Enter to close: ' _ || true; fi; exit 1; }
[ "$(id -u)" != 0 ] || fail 'Run setup as your normal user, not root. Only OS package installation uses authentication.'
os=$(uname -s)
arch=$(uname -m)
case "$os/$arch" in
    Darwin/x86_64|Darwin/arm64)
        version=$(sw_vers -productVersion)
        major=${version%%.*}; minor=${version#*.}; minor=${minor%%.*}
        [ "$major" -gt 14 ] || { [ "$major" = 14 ] && [ "$minor" -ge 4 ]; } || fail 'macOS 14.4 or later is required.'
        state="$HOME/Library/Application Support/MyAnalysis/setup" ;;
    Linux/x86_64)
        [ -f /etc/os-release ] || fail 'Debian/Ubuntu is required.'
        . /etc/os-release
        case "$ID" in
            ubuntu) [ "${VERSION_ID%%.*}" -ge 24 ] || fail 'Ubuntu 24.04 or later is required.' ;;
            debian) [ "${VERSION_ID%%.*}" -ge 12 ] || fail 'Debian 12 or later is required.' ;;
            *) fail 'Automatic installation supports Ubuntu/Debian.' ;;
        esac
        state="${XDG_STATE_HOME:-$HOME/.local/state}/myanalysis/setup" ;;
    *) fail 'Supported targets: macOS Intel/Apple Silicon, Debian/Ubuntu amd64.' ;;
esac
mkdir -p "$state"
# A native shell lock is held before Python exists. mkdir is not used as a stale lock.
if [ "$os" = Linux ]; then
    exec 9>"$state/bootstrap.lock"
    flock -n 9 || fail 'Another bootstrap is running. Retry when it finishes.'
fi
python=''
for candidate in /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    [ -x "$candidate" ] || continue
    # Kill an unresponsive detection, rather than interpreting it as missing.
    "$candidate" -I -c 'import sys,struct,sysconfig; sys.exit(0 if sys.version_info>=(3,11) and struct.calcsize("P")==8 and not sysconfig.get_config_var("Py_GIL_DISABLED") else 1)' & probe_pid=$!
    probe_end=$((SECONDS+10))
    while kill -0 "$probe_pid" 2>/dev/null; do
        if [ "$SECONDS" -ge "$probe_end" ]; then kill "$probe_pid" 2>/dev/null || true; wait "$probe_pid" 2>/dev/null || true; fail "Python detection timed out: $candidate"; fi
        sleep .1
    done
    if wait "$probe_pid"; then python=$candidate; break; fi
done
if [ -z "$python" ]; then
    if [ "$os" = Darwin ]; then
        url='https://www.python.org/ftp/python/3.13.16/python-3.13.16-macos11.pkg'
        sha='30666509020b4da0dd8bc2e773255f34d76b7bb80b66960a928d5f6daa0192d7'
        pkg="$state/python-3.13.16-macos11.pkg"
        valid_pkg() { [ -f "$pkg" ] && [ "$(stat -f %z "$pkg")" = 70394335 ] && [ "$(shasum -a 256 "$pkg" | cut -d ' ' -f 1)" = "$sha" ]; }
        if ! valid_pkg; then
            part=$(mktemp "$state/python.XXXXXX")
            curl --fail --location --proto '=https' --proto-redir '=https' --connect-timeout 15 --speed-time 60 --speed-limit 1 --max-time 900 --retry 2 --output "$part" "$url" || fail 'Official Python download failed. Rerun setup to resume.'
            [ "$(stat -f %z "$part")" = 70394335 ] && [ "$(shasum -a 256 "$part" | cut -d ' ' -f 1)" = "$sha" ] || fail 'Python package checksum/size mismatch.'
            mv "$part" "$pkg"
        fi
        if pgrep -x Installer >/dev/null 2>&1; then fail 'An OS installer is already open. Finish it and rerun setup.'; fi
        echo 'Opening the official Python installer. Complete its dialogs; setup monitors progress for 30 minutes.'
        # Installer.app owns the operation and survives closing this terminal.
        open -W "$pkg" & installer_monitor=$!
        end=$((SECONDS+1800))
        while kill -0 "$installer_monitor" 2>/dev/null; do
            [ "$SECONDS" -lt "$end" ] || fail 'Python installer is still open. Finish it, then rerun setup.'
            echo 'Waiting for the OS Python installer...'; sleep 10
        done
        python='/Library/Frameworks/Python.framework/Versions/3.13/bin/python3'
        [ -x "$python" ] || fail 'Python was not installed. Rerun setup after finishing the OS installer.'
        # python.org's certificate helper is scoped to its installation.
        cert='/Applications/Python 3.13/Install Certificates.command'
        if [ -f "$cert" ]; then /bin/sh "$cert" || fail 'Python certificate setup failed; see Install Certificates.command.'; fi
    else
        echo 'Python 3.11+ is missing. Authenticate to install the missing OS packages.'
        if [ -f "$state/python-apt.pid" ]; then
            pending_pid=$(cat "$state/python-apt.pid")
            if kill -0 "$pending_pid" 2>/dev/null; then fail 'The previous apt installation is still running. Wait for it, then rerun setup.'; fi
        fi
        log="$state/python-apt.log"
        if command -v pkexec >/dev/null 2>&1; then
            nohup pkexec env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y python3 python3-venv >"$log" 2>&1 </dev/null & apt_pid=$!
        else
            # Bound authentication separately; never kill the package manager.
            sudo -v & auth_pid=$!
            auth_end=$((SECONDS+600))
            while kill -0 "$auth_pid" 2>/dev/null; do
                if [ "$SECONDS" -ge "$auth_end" ]; then kill "$auth_pid" 2>/dev/null || true; fail 'Authentication timed out. Rerun setup to retry.'; fi
                sleep 1
            done
            wait "$auth_pid" || fail 'OS package authentication cancelled.'
            nohup sudo -n env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y python3 python3-venv >"$log" 2>&1 </dev/null & apt_pid=$!
        fi
        echo "$apt_pid" >"$state/python-apt.pid"
        end=$((SECONDS+1800))
        while kill -0 "$apt_pid" 2>/dev/null; do
            [ "$SECONDS" -lt "$end" ] || fail "OS installation continues; log: $log. Finish it and rerun setup."
            echo "Waiting for Python OS packages; log: $log"; sleep 2
        done
        wait "$apt_pid" || fail "Python package installation failed; see $log and rerun setup."
        python=/usr/bin/python3
    fi
fi
core=$(mktemp "$state/core.XXXXXX")
"$python" -I -c 'import base64,pathlib,sys;pathlib.Path(sys.argv[1]).write_bytes(base64.b64decode(sys.argv[2]))' "$core" '__CORE_BASE64__'
args=()
[ -z "$install_dir" ] || args+=(--install-dir "$install_dir")
[ "$no_pause" = 0 ] || args+=(--no-pause)
[ "$no_launch" = 0 ] || args+=(--no-launch)
source_dir=$(cd -- "$(dirname -- "$0")" && pwd)
args+=(--source-dir "$source_dir")
if [ "$os" = Linux ]; then flock -u 9; exec 9>&-; fi
exec "$python" -u "$core" "${args[@]}"
