"""Build standalone entry points and native launcher packages (no downloads).

python -m installer.build                 # refresh setup.bat/setup.sh
python -m installer.build --deb --out dist
python -m installer.build --pkg --out dist # macOS, unsigned
"""
from __future__ import annotations

import argparse
import base64
import os
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def entrypoints(out):
    out.mkdir(parents=True, exist_ok=True)
    # Git may check Python files out with CRLF on Windows. Normalize the
    # embedded source so release entry points are reproducible on every OS.
    source = (ROOT / "installer/core.py").read_text(encoding="utf-8").encode("utf-8")
    payload = base64.b64encode(source).decode("ascii")
    ps = (ROOT / "installer/windows.ps1").read_text(encoding="utf-8").replace("__CORE_BASE64__", payload)
    ps64 = base64.b64encode(ps.encode("utf-8")).decode("ascii")
    # cmd only parses these ASCII lines. Read embedded data from our own file.
    bat = '''@echo off
setlocal DisableDelayedExpansion
set "MYANALYSIS_SETUP_FILE=%~f0"
set "MYANALYSIS_SETUP_SOURCE=%~dp0"
rem Optional CLI: setup.bat --install-dir PATH --no-pause --no-launch
:args
if "%~1"=="" goto run
if /i "%~1"=="--install-dir" goto directory
if /i "%~1"=="--no-pause" goto nopause
if /i "%~1"=="--no-launch" goto nolaunch
echo Unknown setup option.
exit /b 2
:nopause
set "MYANALYSIS_NO_PAUSE=1"
shift
goto args
:nolaunch
set "MYANALYSIS_NO_LAUNCH=1"
shift
goto args
:directory
if "%~2"=="" exit /b 2
set "MYANALYSIS_INSTALL_DIR=%~2"
shift
shift
goto args
:run
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$t=[IO.File]::ReadAllText($env:MYANALYSIS_SETUP_FILE);$p=$t.Substring($t.LastIndexOf('::PAYLOAD:')+10).Trim();& ([scriptblock]::Create([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($p))))"
set "MYANALYSIS_SETUP_EXIT=%errorlevel%"
if "%MYANALYSIS_SETUP_EXIT%"=="0" exit /b 0
if "%MYANALYSIS_NO_PAUSE%"=="1" exit /b %MYANALYSIS_SETUP_EXIT%
echo Setup did not complete. Read the message above, then rerun setup to resume.
pause
exit /b %MYANALYSIS_SETUP_EXIT%
::PAYLOAD:''' + ps64 + "\n"
    # Keep individual payload lines within cmd/Windows text limits. Only PS reads them.
    prefix, data = bat.rsplit("::PAYLOAD:", 1)
    bat = prefix + "::PAYLOAD:\n" + "\n".join(data.strip()[i:i+120] for i in range(0, len(data.strip()), 120)) + "\n"
    (out / "setup.bat").write_bytes(bat.replace("\n", "\r\n").encode("ascii"))
    shell = (ROOT / "installer/unix.sh").read_text(encoding="utf-8").replace("__CORE_BASE64__", payload)
    (out / "setup.sh").write_text(shell, encoding="utf-8", newline="\n")
    (out / "setup.sh").chmod(0o755)


def deb(out):
    with tempfile.TemporaryDirectory() as temporary:
        tree = Path(temporary)
        control = tree / "DEBIAN"
        control.mkdir()
        (control / "control").write_text("Package: myanalysis\nVersion: 1.0.0\nSection: science\nPriority: optional\nArchitecture: amd64\nMaintainer: MyAnalysis contributors\nDepends: x-terminal-emulator, policykit-1, util-linux, curl, ca-certificates\nDescription: MyAnalysis launcher and first-run bootstrap\n", encoding="utf-8")
        target = tree / "usr/share/myanalysis"
        entrypoints(target)
        (target / "setup.bat").unlink()
        applications = tree / "usr/share/applications"
        applications.mkdir(parents=True)
        (applications / "myanalysis.desktop").write_text("[Desktop Entry]\nType=Application\nName=MyAnalysis\nExec=/usr/share/myanalysis/setup.sh\nTerminal=true\nCategories=Science;\n", encoding="utf-8")
        subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(tree), str(out / "MyAnalysis.deb")], check=True)


def pkg(out):
    with tempfile.TemporaryDirectory() as temporary:
        tree = Path(temporary)
        app = tree / "Applications/MyAnalysis.app/Contents"
        executable = app / "MacOS"
        resources = app / "Resources"
        executable.mkdir(parents=True)
        entrypoints(resources)
        (resources / "setup.bat").unlink()
        with (app / "Info.plist").open("wb") as f:
            plistlib.dump(dict(CFBundleIdentifier="org.myanalysis.launcher", CFBundleName="MyAnalysis",
                               CFBundleExecutable="MyAnalysis", CFBundleVersion="1", CFBundlePackageType="APPL",
                               LSMinimumSystemVersion="14.4"), f)
        launcher = executable / "MyAnalysis"
        # Pass paths as AppleScript arguments and quote them for Terminal. The
        # installed app may be relocated or live on a volume with spaces.
        launcher.write_text('''#!/bin/sh
resources=$(CDPATH= cd -- "$(dirname -- "$0")/../Resources" && pwd) || exit 1
exec /usr/bin/osascript - "$resources/setup.sh" <<'APPLESCRIPT'
on run argv
    tell application "Terminal"
        do script "/bin/bash " & quoted form of (item 1 of argv)
        activate
    end tell
end run
APPLESCRIPT
''', encoding="utf-8")
        launcher.chmod(0o755)
        subprocess.run(["pkgbuild", "--root", str(tree), "--identifier", "org.myanalysis.launcher",
                        "--version", "1.0.0", "--install-location", "/", str(out / "MyAnalysis.pkg")], check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT)
    parser.add_argument("--deb", action="store_true")
    parser.add_argument("--pkg", action="store_true")
    args = parser.parse_args()
    entrypoints(args.out)
    if args.deb:
        deb(args.out)
    if args.pkg:
        pkg(args.out)


if __name__ == "__main__":
    main()
