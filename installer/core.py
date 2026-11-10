"""Resumable per-user installer. This file also runs outside a checkout.

The release entry points embed this exact source. No Qt or third-party imports
are allowed here. A detached worker owns locks and children, independently of
the terminal that displays its log.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import errno
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

API = 1
URL = "https://github.com/atakalive/MyAnalysis.git"
GIT = {
    "url": "https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.4/Git-2.56.0.4-64-bit.exe",
    "size": 66850032,
    "sha256": "4961a4a8cc778d8b6e00a5819e5fe4d078b754555aff7aedc2d77a5d74c2de80",
}
PYTHON_MAC = {
    "url": "https://www.python.org/ftp/python/3.13.16/python-3.13.16-macos11.pkg",
    "size": 70394335,
    "sha256": "30666509020b4da0dd8bc2e773255f34d76b7bb80b66960a928d5f6daa0192d7",
}
MAX_OUTPUT = 1024 * 1024


class SetupError(RuntimeError):
    pass


def say(message):
    try:
        print(message, flush=True)
    except UnicodeError:
        with contextlib.suppress(OSError, UnicodeError):
            encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
            print(str(message).encode(encoding, "backslashreplace").decode(encoding), flush=True)
    except OSError:
        pass  # A closed terminal must not interrupt a mutation.


def state_root():
    if os.environ.get("MYANALYSIS_SETUP_HOME"):
        return Path(os.environ["MYANALYSIS_SETUP_HOME"])
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "MyAnalysis/setup"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/MyAnalysis/setup"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "myanalysis/setup"


def default_target():
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Programs/MyAnalysis"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/MyAnalysis/app"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "myanalysis/app"


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def key_for(repo):
    return hashlib.sha256(os.path.normcase(str(Path(repo).resolve())).encode()).hexdigest()


@contextlib.contextmanager
def mutation_lock(repo, timeout=0):
    """OS lock, shared by setup, updater and optional dependency installation."""
    root = state_root() / "locks"
    root.mkdir(parents=True, exist_ok=True)
    with (root / (key_for(repo) + ".lock")).open("a+b") as handle:
        handle.seek(0)
        end = time.monotonic() + timeout
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= end:
                    raise SetupError("Another setup/update is using this installation. Retry after it finishes.") from exc
                time.sleep(.2)
        try:
            yield
        finally:
            if sys.platform == "win32":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def capture(argv, timeout=10, cwd=None, env=None):
    try:
        result = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=timeout, **quiet())
    except subprocess.TimeoutExpired as exc:
        raise SetupError(f"Detection timed out (not treated as missing): {argv[0]}") from exc
    except OSError as exc:
        raise SetupError(f"Cannot execute {argv[0]}: {exc}") from exc
    if result.returncode:
        raise SetupError(result.stderr[-8192:] or result.stdout[-8192:] or f"Exit {result.returncode}: {argv[0]}")
    return result.stdout.strip()


def quiet():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def detached():
    if sys.platform == "win32":
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def process_stamp(pid):
    """Creation identity, not merely PID existence. Unknown fails closed."""
    try:
        if sys.platform == "win32":
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = wintypes.HANDLE
            handle = kernel.OpenProcess(0x1000, False, int(pid))
            if not handle:
                if ctypes.get_last_error() == 87:
                    return None
                return "unknown"
            times = [wintypes.FILETIME() for _ in range(4)]
            try:
                exit_code = wintypes.DWORD()
                if kernel.GetExitCodeProcess(wintypes.HANDLE(handle), ctypes.byref(exit_code)) and exit_code.value != 259:
                    return None
                if not kernel.GetProcessTimes(wintypes.HANDLE(handle), *(ctypes.byref(t) for t in times)):
                    return "unknown"
                return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
            finally:
                kernel.CloseHandle(wintypes.HANDLE(handle))
        result = capture(["ps", "-p", str(pid), "-o", "lstart="], 10)
        return result or None
    except SetupError:
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return None
        except OSError:
            pass
        return "unknown"


def alive(record):
    stamp = process_stamp(record.get("pid", -1))
    return stamp is not None and (stamp == "unknown" or stamp == record.get("stamp"))


def kill_command(proc):
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, timeout=10, **quiet())
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired as exc:
        raise SetupError("Child remains running; retry is blocked until it exits.") from exc


class CommandJob:
    """Windows job: normal children cannot survive a killed worker.

    OS installers deliberately do not join this kill-on-close job.
    POSIX uses a recorded process group, checked before any subsequent mutation.
    """
    def __init__(self, proc, enabled):
        self.handle = None
        if sys.platform != "win32" or not enabled:
            return
        from ctypes import wintypes as w
        class BASIC(ctypes.Structure):
            _fields_ = [("per_process", ctypes.c_int64), ("per_job", ctypes.c_int64),
                        ("flags", w.DWORD), ("min_work", ctypes.c_size_t), ("max_work", ctypes.c_size_t),
                        ("active", w.DWORD), ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class EXTENDED(ctypes.Structure):
            _fields_ = [("basic", BASIC), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.handle = self.kernel.CreateJobObjectW(None, None)
        info = EXTENDED()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not self.handle or not self.kernel.SetInformationJobObject(w.HANDLE(self.handle), 9, ctypes.byref(info), ctypes.sizeof(info))
                or not self.kernel.AssignProcessToJobObject(w.HANDLE(self.handle), w.HANDLE(int(proc._handle)))):
            self.close()
            kill_command(proc)
            raise SetupError("Could not attach command to a supervised Windows job; no retry started")

    def close(self):
        if self.handle:
            from ctypes import wintypes
            self.kernel.CloseHandle(wintypes.HANDLE(self.handle))
            self.handle = None

    def __del__(self):
        self.close()


def run(argv, *, cwd=None, timeout=600, env=None, os_install=False, cancelled=None, progress=None):
    """Spool to disk rather than pipes: bounded memory and no pipe deadlock.

    OS installation outlives its monitor deadline and keeps the worker's lock.
    Ctrl+C only stops a frontend, never an OS package manager.
    """
    jobs = state_root() / "children"
    jobs.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    log = jobs / (token + ".log")
    record_path = jobs / (token + ".json")
    say("Running: " + " ".join(str(a) for a in argv[:4]))
    with log.open("wb") as output:
        proc = subprocess.Popen([str(a) for a in argv], cwd=cwd, env=env,
                                stdin=None if os_install else subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.STDOUT,
                                start_new_session=(sys.platform != "win32" and not os_install), **quiet())
        job = CommandJob(proc, not os_install)
        record = {"pid": proc.pid, "stamp": process_stamp(proc.pid),
                  "target": str(cwd or "OS"), "os_install": os_install,
                  "owner": os.getpid(), "started": time.time(),
                  "run_result": os.environ.get("MYANALYSIS_SETUP_RESULT")}
        write_json(record_path, record)
        start = last = time.monotonic()
        tail = bytearray()
        with log.open("rb") as reader:
            while True:
                chunk = reader.read(65536)
                if chunk:
                    tail.extend(chunk)
                    del tail[:-MAX_OUTPUT]
                    say(chunk.decode("utf-8", errors="replace").rstrip())
                    if progress:
                        progress(chunk.decode("utf-8", errors="replace").rstrip())
                if proc.poll() is not None:
                    while rest := reader.read(65536):
                        tail.extend(rest)
                        del tail[:-MAX_OUTPUT]
                        say(rest.decode("utf-8", errors="replace").rstrip())
                    break
                now = time.monotonic()
                if cancelled and cancelled() and not os_install:
                    kill_command(proc)
                    raise SetupError("Command cancelled; environment may be partial. Rerun to recheck.")
                if now - last >= 10:
                    say(f"Waiting for {Path(argv[0]).name}: {int(now-start)}s; log: {log}")
                    last = now
                if now - start >= timeout:
                    if not os_install:
                        kill_command(proc)
                        raise SetupError(f"Timed out: {argv[0]}; log: {log}")
                    say("OS installer still running. Close this monitor if needed; rerun after installation finishes.")
                    record["monitor_expired"] = True
                    write_json(record_path, record)
                    timeout = float("inf")
                time.sleep(.1)
        record_path.unlink(missing_ok=True)
        job.close()
        if not os_install and sys.platform != "win32":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
    if proc.returncode not in ((0, 3010) if os_install else (0,)):
        raise SetupError(f"Exit {proc.returncode}: {argv[0]}\n{tail.decode('utf-8', errors='replace')[-8192:]}\nLog: {log}")
    if proc.returncode == 3010:
        raise SetupError("OS installation requires a restart. Restart manually, then run setup again.")
    return tail.decode("utf-8", errors="replace").strip()


def check_children():
    for path in (state_root() / "children").glob("*.json"):
        record = read_json(path)
        group_alive = False
        if record and sys.platform != "win32":
            try:
                os.killpg(record["pid"], 0)
                group_alive = True
            except ProcessLookupError:
                pass
            except PermissionError:
                group_alive = True
        if record and (alive(record) or group_alive):
            raise SetupError(f"A previous installation command is still running: {record.get('target')}. Log: {path.with_suffix('.log')}")


def fetch(asset):
    root = state_root() / "downloads"
    root.mkdir(parents=True, exist_ok=True)
    path = root / asset["url"].rsplit("/", 1)[-1]

    def valid(p):
        if not p.is_file() or p.stat().st_size != asset["size"]:
            return False
        with p.open("rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest() == asset["sha256"]

    if valid(path):
        say(f"Skip download: verified cache {path.name}")
        return path
    part = path.with_name(path.name + "." + uuid.uuid4().hex + ".part")
    for attempt in range(3):
        try:
            start = time.monotonic()
            req = urllib.request.Request(asset["url"], headers={"User-Agent": "MyAnalysis-setup/1"})
            with urllib.request.urlopen(req, timeout=15) as response, part.open("wb") as out:
                if not response.url.startswith("https://"):
                    raise SetupError("Download redirected away from HTTPS")
                # urlopen's socket timeout also bounds reads. HTTPS proxies and SSL_CERT_FILE are honored.
                while chunk := response.read(65536):
                    out.write(chunk)
                    if out.tell() > asset["size"] or time.monotonic() - start > 900:
                        raise SetupError("Download exceeded size/time limit")
            if not valid(part):
                raise SetupError("Installer SHA-256 or size mismatch")
            os.replace(part, path)
            return path
        except (TimeoutError, urllib.error.URLError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code not in (408, 429, 500, 502, 503, 504):
                raise SetupError(f"Download refused: {exc}") from exc
            if attempt == 2:
                raise SetupError(f"Download failed: {exc}") from exc
            say(f"Temporary network failure; attempt {attempt+2}/3")
        finally:
            part.unlink(missing_ok=True)


PYTHON_INFO = r'''
import json,sys,sysconfig,struct,platform
print(json.dumps(dict(executable=sys.executable,base=sys.base_prefix,prefix=sys.prefix,
 version=list(sys.version_info[:3]),bits=struct.calcsize("P")*8,machine=platform.machine(),
 abi=sysconfig.get_config_var("SOABI") or ("cp"+str(sys.version_info.major)+str(sys.version_info.minor)),os=platform.platform(),
 gil_disabled=bool(sysconfig.get_config_var("Py_GIL_DISABLED")),implementation=sys.implementation.name)))
'''


def python_info(executable):
    return json.loads(capture([str(executable), "-I", "-c", PYTHON_INFO]))


def supported_python(info):
    if info.get("implementation") != "cpython" or info.get("bits") != 64 or info.get("gil_disabled"):
        return False
    if tuple(info.get("version", [])[:2]) < (3, 11):
        return False
    if sys.platform == "win32":
        return not any("windowsapps" in str(info.get(k, "")).lower() for k in ("base", "prefix", "executable"))
    return True


def venv_python(repo):
    return Path(repo) / (".venv/Scripts/python.exe" if sys.platform == "win32" else ".venv/bin/python")


def select_target(explicit=None, source=None):
    if explicit:
        return Path(explicit).expanduser().absolute()
    saved = read_json(state_root() / "selection.json").get("target")
    candidates = [Path(saved)] if saved else []
    if source and (Path(source) / ".git").exists():
        candidates.append(Path(source))
    candidates.extend([default_target(), Path.home() / "MyAnalysis"])
    return next((p.absolute() for p in candidates if p.exists()), default_target().absolute())


def git_environment():
    env = dict(os.environ)
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_ASKPASS="", SSH_ASKPASS="",
               PIP_NO_INPUT="1", PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONIOENCODING="utf-8")
    return env


def find_git():
    candidates = [shutil.which("git")]
    if sys.platform == "win32":
        candidates += [str(Path(root) / "Git/cmd/git.exe") for root in
                       (os.environ.get("LOCALAPPDATA", "") + "/Programs", os.environ.get("ProgramFiles", "C:/Program Files"))]
        try:
            import winreg
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, r"Software\GitForWindows") as k:
                        candidates.append(str(Path(winreg.QueryValueEx(k, "InstallPath")[0]) / "cmd/git.exe"))
                except OSError:
                    pass
        except ImportError:
            pass
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            if sys.platform == "darwin" and candidate == "/usr/bin/git":
                try:
                    capture(["xcode-select", "-p"])
                except SetupError:
                    continue
            capture([candidate, "--version"])
            bash = None
            if sys.platform == "win32":
                root = Path(candidate).resolve().parent.parent
                bash = next((p for p in (root / "bin/bash.exe", root / "usr/bin/bash.exe") if p.is_file()), None)
                if bash:
                    capture([str(bash), "--noprofile", "--norc", "-c", "exit 0"])
                else:
                    continue
            return str(Path(candidate).resolve()), str(bash) if bash else None
    return None, None


def apt_install(packages):
    if not shutil.which("apt-get"):
        raise SetupError("Automatic OS dependency installation supports Debian/Ubuntu (apt-get) only.")
    lock_probe = "fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock >/dev/null 2>&1"
    deadline = time.monotonic() + 600
    while subprocess.run(["sh", "-c", lock_probe]).returncode == 0:
        if time.monotonic() > deadline:
            raise SetupError("Package manager is busy. Retry after the other installation finishes.")
        say("Waiting for another package manager (Ctrl+C closes the monitor).")
        time.sleep(10)
    # pkexec provides an OS authentication dialog even for detached workers.
    if shutil.which("pkexec") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        elevation = ["pkexec"]
    elif shutil.which("sudo") and os.environ.get("MYANALYSIS_SETUP_TTY"):
        # Authentication belongs to the foreground UI. The worker never reads a
        # password or leaves a background sudo competing with the shell's input.
        result_file = os.environ.get("MYANALYSIS_SETUP_RESULT")
        if not result_file:
            raise SetupError("Run setup.sh in an interactive terminal for OS authentication")
        auth = Path(result_file).with_suffix(".auth.json")
        answer = auth.with_suffix(".answer.json")
        request_id = uuid.uuid4().hex
        write_json(auth, {"id": request_id, "packages": packages})
        end = time.monotonic() + 600
        while read_json(answer).get("id") != request_id:
            if time.monotonic() >= end:
                raise SetupError("OS authentication monitor closed or timed out; no package installation was started")
            time.sleep(.2)
        if not read_json(answer).get("ok"):
            raise SetupError("OS package authentication was cancelled or refused")
        elevation = ["sudo", "-n"]
    else:
        raise SetupError("OS authentication is unavailable. Run in an interactive terminal: sudo apt-get install " + " ".join(packages) + "; then rerun setup.")
    run([*elevation, "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "-o", "DPkg::Lock::Timeout=600",
         "install", "-y", *packages], os_install=True, timeout=1800)


def ensure_git():
    git, bash = find_git()
    if git:
        say("Skip Git: executable and Git Bash are usable" if bash else "Skip Git: usable")
        return git, bash
    with mutation_lock("OS-toolchain", 600):
        git, bash = find_git()
        if not git:
            check_children()
            if sys.platform == "win32":
                exe = fetch(GIT)
                target = Path(os.environ["LOCALAPPDATA"]) / "Programs/Git"
                run([str(exe), "/VERYSILENT", "/NORESTART", "/CURRENTUSER", "/SP-", "/SUPPRESSMSGBOXES",
                     "/o:PathOption=BashOnly", f"/DIR={target}"], timeout=1800, os_install=True)
            elif sys.platform == "darwin":
                # xcode-select only opens the dialog; it does not wait for CLT.
                pending = state_root() / "clt-request.json"
                if not pending.exists():
                    subprocess.run(["xcode-select", "--install"], capture_output=True, timeout=10)
                    write_json(pending, {"requested": time.time()})
                deadline = time.monotonic() + 1800
                while time.monotonic() < deadline:
                    git, bash = find_git()
                    if git:
                        pending.unlink(missing_ok=True)
                        return git, bash
                    say("Waiting for Command Line Tools installation; complete the OS dialog.")
                    time.sleep(10)
                raise SetupError("Command Line Tools not ready. Finish the OS dialog (or reopen with xcode-select --install), then rerun setup.")
            else:
                apt_install(["git"])
    git, bash = find_git()
    if not git:
        raise SetupError("Git installation is not complete. See OS installer log and rerun setup.")
    return git, bash


def inspect_repo(repo, git):
    if not (repo / "tool.py").is_file() or not (repo / "requirements.txt").is_file():
        raise SetupError(f"Existing directory is not a usable checkout; preserved: {repo}. Choose --install-dir.")
    env = git_environment()
    head = capture([git, "-C", str(repo), "rev-parse", "--verify", "HEAD"], env=env)
    branch = capture([git, "-C", str(repo), "symbolic-ref", "--short", "HEAD"], env=env)
    origins = capture([git, "-C", str(repo), "remote", "-v"], env=env).lower()
    def project_remote(url):
        if "://" in url:
            parsed = urllib.parse.urlsplit(url)
            host, path = parsed.hostname, parsed.path.lstrip("/")
        else:
            match = re.fullmatch(r"(?:git@)?(github\.com|gitlab\.com):(.+)", url)
            if not match:
                return False
            host, path = match.groups()
        return host in ("github.com", "gitlab.com") and path.rstrip("/").removesuffix(".git") == "atakalive/myanalysis"
    remotes = [line.split()[1] for line in origins.splitlines() if len(line.split()) >= 2]
    if branch != "main" or not any(project_remote(url) for url in remotes):
        raise SetupError("Existing checkout has a different source/branch; preserved. Choose --install-dir.")
    return head


def install_checkout(repo, git):
    if repo.exists():
        return inspect_repo(repo, git)
    if repo.is_symlink():
        raise SetupError("Refusing a symbolic link as a new installation destination")
    validate_new_target(repo)
    repo.parent.mkdir(parents=True, exist_ok=True)
    stage = repo.with_name(repo.name + ".setup-clone")
    marker = stage.with_suffix(stage.suffix + ".owner.json")
    identity = {"api": API, "target": str(repo)}
    if stage.exists():
        if read_json(marker) != identity:
            raise SetupError(f"Unowned staging directory preserved: {stage}")
        try:
            inspect_repo(stage, git)
        except SetupError:
            resume_clone(stage, git)
    write_json(marker, identity)
    if not stage.exists():
        for attempt in range(3):
            try:
                run([git, "-c", "credential.helper=", "-c", "core.askPass=", "clone", "--branch", "main", "--single-branch", URL, str(stage)],
                    timeout=600, env=git_environment())
                break
            except SetupError as exc:
                if attempt == 2 or not re.search(r"timed out|resolve host|reset|HTTP (?:429|50[234])|early EOF", str(exc), re.I):
                    raise
                if stage.exists():
                    resume_clone(stage, git)
                    if stage.exists():
                        break
    head = inspect_repo(stage, git)
    if repo.exists():
        raise SetupError("Destination appeared during clone; both directories preserved")
    stage.rename(repo)
    marker.unlink(missing_ok=True)
    return head


def resume_clone(stage, git):
    """Recover an interrupted unborn checkout without deleting its contents."""
    if not any(stage.iterdir()):
        stage.rmdir()  # Empty installer-owned directory only.
        return
    if not (stage / ".git").is_dir() or (stage / ".git").is_symlink():
        raise SetupError(f"Incomplete staging directory contains unknown files; preserved: {stage}. Choose --install-dir.")
    try:
        capture([git, "-C", str(stage), "rev-parse", "--verify", "HEAD"], env=git_environment())
    except SetupError:
        run([git, "-C", str(stage), "-c", "credential.helper=", "-c", "core.askPass=", "fetch", "--no-tags", URL,
             "refs/heads/main:refs/remotes/origin/main"], timeout=600, env=git_environment())
        # checkout refuses to overwrite untracked files. No reset, clean or -f.
        run([git, "-C", str(stage), "checkout", "-b", "main", "--track", "origin/main"], timeout=120, env=git_environment())
        inspect_repo(stage, git)
        return
    raise SetupError(f"Staging checkout has a HEAD but is incomplete; contents preserved: {stage}. Choose --install-dir.")


def validate_new_target(repo):
    """A downloaded entry point may be on a sync drive; a new app must not be."""
    absolute = Path(repo).absolute()
    for parent in (absolute, *absolute.parents):
        if parent.is_symlink():
            raise SetupError("New installations require a local directory without symbolic links")
    lower = str(absolute).replace("\\", "/").lower()
    sync_roots = [os.environ.get(name) for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")]
    if sys.platform == "darwin":
        sync_roots.append(str(Path.home() / "Library/CloudStorage"))
    if any(root and (lower == str(root).replace("\\", "/").lower() or lower.startswith(str(root).replace("\\", "/").lower().rstrip("/") + "/")) for root in sync_roots):
        raise SetupError("Choose a local --install-dir outside the sync folder")
    if sys.platform == "win32":
        drive = absolute.anchor
        kind = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(drive))
        if kind not in (2, 3):
            raise SetupError("Choose a local disk for --install-dir; network drives are not supported for a new installation")
    elif sys.platform.startswith("linux"):
        mounts = Path("/proc/mounts")
        if mounts.is_file():
            matches = []
            for line in mounts.read_text().splitlines():
                fields = line.split()
                if len(fields) < 3:
                    continue
                mount = fields[1].replace("\\040", " ").rstrip("/") or "/"
                if str(absolute) == mount or str(absolute).startswith(mount.rstrip("/") + "/"):
                    matches.append((len(mount), fields[2]))
            kind = max(matches, default=(0, ""))[1]
            if kind.startswith(("fuse", "nfs")) or kind in ("cifs", "smbfs", "9p", "drvfs"):
                raise SetupError("Choose a local filesystem for --install-dir")


DEPENDENCY_PROBE = r'''
import importlib.metadata as m,json
from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.utils import canonicalize_name
all_dists={canonicalize_name(d.metadata["Name"]):d for d in m.distributions() if d.metadata["Name"]}
missing=[]; conflicts=[]; visited=set()
def visit(text, extras=()):
 r=Requirement(text)
 if r.marker and not any(r.marker.evaluate({"extra":extra}) for extra in ("",*extras)): return
 name=canonicalize_name(r.name)
 d=all_dists.get(name)
 if d is None: missing.append(text); return
 if r.specifier and not r.specifier.contains(d.version,prereleases=True): conflicts.append(text+" (installed "+d.version+")")
 key=(name,tuple(sorted(r.extras)))
 if key in visited: return
 visited.add(key)
 for child in d.requires or []: visit(child,r.extras)
for text in __import__("pathlib").Path(__import__("sys").argv[1]).read_text().splitlines():
 if text.strip() and not text.lstrip().startswith("#"): visit(text)
print(json.dumps(dict(missing=missing,conflicts=conflicts,installed={k:d.version for k,d in all_dists.items()})))
'''
IMPORT_PROBE = "import PySide6.QtCore, PySide6.QtWidgets, pyqtgraph, numpy, pandas, matplotlib, PIL; print('imports OK')"
QT_PROBE = "from PySide6.QtWidgets import QApplication; app=QApplication([]); app.processEvents(); print('Qt OK')"


def ensure_dependencies(repo, python):
    deadline = time.monotonic() + 1800
    def pip_run(argv):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SetupError("Dependency installation exceeded 30 minutes; rerun setup to recheck")
        return run(argv, timeout=remaining, env=git_environment())
    requirements = repo / "requirements.txt"
    try:
        probe = json.loads(capture([str(python), "-I", "-c", DEPENDENCY_PROBE, str(requirements)], 120))
    except SetupError as exc:
        raise SetupError(f"Dependency inspection failed; existing venv preserved: {exc}") from exc
    if probe["conflicts"]:
        raise SetupError("Existing dependency conflicts; environment preserved: " + "; ".join(probe["conflicts"]))
    if probe["missing"]:
        setup = repo / "data/setup"
        setup.mkdir(parents=True, exist_ok=True)
        constraints = setup / "constraints.txt"
        # Exact constraints prevent pip from silently updating any existing distribution.
        constraints.write_text("\n".join(f"{k}=={v}" for k, v in sorted(probe["installed"].items())), encoding="utf-8")
        report = setup / "resolution.json"
        wheels = setup / "wheels"
        wheels.mkdir(exist_ok=True)
        args = [str(python), "-m", "pip", "--disable-pip-version-check"]
        opts = ["--only-binary=:all:", "--no-input", "--timeout=60", "--retries=2", "-c", str(constraints), "-r", str(requirements)]
        pip_run([*args, "install", "--dry-run", "--report", str(report), *opts])
        resolution = read_json(report)
        additions = resolution.get("install", [])
        # Download exactly the resolver's additions, then install without networking.
        for item in additions:
            name, version = item["metadata"]["name"], item["metadata"]["version"]
            if re.sub(r"[-_.]+", "-", name).lower() in probe["installed"]:
                raise SetupError("Resolver would change an existing distribution; environment preserved")
            pip_run([*args, "download", "--only-binary=:all:", "--no-deps", "--no-input", "--timeout=60", "--retries=2",
                     "--dest", str(wheels), f"{name}=={version}"])
        pip_run([*args, "install", "--no-index", "--find-links", str(wheels), *opts])
        probe = json.loads(capture([str(python), "-I", "-c", DEPENDENCY_PROBE, str(requirements)], 120))
        if probe["missing"] or probe["conflicts"]:
            raise SetupError("Dependency verification failed after pip; rerun to inspect partial installation")
    else:
        say("Skip pip: all required and transitive dependencies are installed")
    capture([str(python), "-I", "-c", IMPORT_PROBE], 120)
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    capture([str(python), "-I", "-c", QT_PROBE], 120, env=env)
    return probe["installed"]


def app_records(repo):
    return Path(repo) / "data/setup/apps"


def wait_for_apps(repo, timeout=600):
    deadline = time.monotonic() + timeout
    while True:
        records = [read_json(p) for p in app_records(repo).glob("*.json")]
        if not any(r and alive(r) for r in records):
            # Old versions do not register; inspect OS command lines too.
            if sys.platform == "win32":
                script = "Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%'\" | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
                raw = capture(["powershell.exe", "-NoProfile", "-Command", script], 10)
                processes = json.loads(raw or "[]")
                if isinstance(processes, dict):
                    processes = [processes]
                lines = [str(p.get("CommandLine") or "") for p in processes if p.get("ProcessId") != os.getpid()]
            else:
                lines = capture(["ps", "-ax", "-o", "command="], 10).splitlines()
            if not any("tool.py" in line and (str(repo).lower() in line.lower() or not any(sep in line for sep in ("/tool.py", "\\tool.py"))) for line in lines):
                return
        if time.monotonic() >= deadline:
            raise SetupError("Close MyAnalysis before repairing this environment, then run setup again.")
        say("Close MyAnalysis to allow repair; waiting up to 10 minutes.")
        time.sleep(2)


def setup(repo):
    with mutation_lock(repo):
        check_children()
        git, bash = ensure_git()
        head = install_checkout(repo, git)
        required_api = repo / "installer/api.json"
        if required_api.exists() and read_json(required_api).get("minimum", 1) > API:
            raise SetupError("This checkout needs a newer bootstrap. Download the latest setup from Releases.")
        runtime = repo / "data/setup/runtime.json"
        python = venv_python(repo)
        info = None
        folder = repo / ".venv"
        if folder.is_symlink() or (hasattr(folder, "is_junction") and folder.is_junction()):
            raise SetupError("Linked .venv preserved; choose an installation with its own local virtual environment")
        if python.is_file():
            cfg = repo / ".venv/pyvenv.cfg"
            home = next((line.partition("=")[2].strip() for line in cfg.read_text(encoding="utf-8").splitlines()
                         if line.partition("=")[0].strip() == "home"), "") if cfg.is_file() else ""
            if home and not Path(home).exists():
                say("Base Python is missing; preserve the broken venv in .venv.setup-backup")
            else:
                try:
                    info = python_info(python)  # Timeout is unknown, never permission to replace.
                except SetupError as exc:
                    if re.search(r"WinError 193|Exec format error|Fatal Python error: init_fs_encoding", str(exc)):
                        say("Confirmed damaged interpreter; preserve the venv before recreating it")
                    else:
                        raise
                if info and os.path.normcase(str(Path(info.get("prefix", "")).resolve())) != os.path.normcase(str(folder.resolve())):
                    raise SetupError("Existing .venv does not run as a virtual environment; preserved. Refusing to modify global Python packages.")
                if info and not supported_python(info):
                    say("Existing venv is unsupported/Store-based; preserve it before creating an official environment")
                    info = None
        if not info:
            wait_for_apps(repo)
            if folder.exists():
                backup = repo / ".venv.setup-backup"
                owner = read_json(repo / "data/setup/venv-owner.json")
                if owner.get("path") == str(folder) and owner.get("incomplete"):
                    say("Reusing this installer's incomplete venv; existing contents are preserved")
                elif not (folder / "pyvenv.cfg").is_file():
                    raise SetupError("Existing .venv cannot be identified; preserved. Choose --install-dir.")
                elif backup.exists():
                    raise SetupError("A previous venv backup exists; both preserved. Choose --install-dir.")
                else:
                    folder.rename(backup)
            creator = sys.executable
            if sys.platform == "darwin" and platform.machine() == "x86_64" and sys.version_info[:2] > (3, 13):
                candidates = [Path(f"/Library/Frameworks/Python.framework/Versions/3.{v}/bin/python3") for v in (13, 12, 11)]
                creator = next((str(p) for p in candidates if p.is_file() and supported_python(python_info(p))), None)
                if not creator:
                    with mutation_lock("OS-toolchain", 600):
                        run(["open", "-W", str(fetch(PYTHON_MAC))], timeout=1800, os_install=True)
                    creator = str(candidates[0])
                    if not Path(creator).is_file():
                        raise SetupError("Official Python 3.13 is not ready; complete the OS installer and rerun setup")
                    certificate_helper = Path("/Applications/Python 3.13/Install Certificates.command")
                    if certificate_helper.is_file():
                        run(["/bin/sh", str(certificate_helper)], timeout=1800, env=git_environment())
            write_json(repo / "data/setup/venv-owner.json", {"path": str(folder), "incomplete": True})
            try:
                run([creator, "-m", "venv", str(folder)], timeout=120)
            except SetupError:
                if sys.platform.startswith("linux"):
                    with mutation_lock("OS-toolchain", 600):
                        apt_install(["python3-venv"])
                    run([creator, "-m", "venv", str(folder)], timeout=120)
                else:
                    raise
            info = python_info(python)
            if not supported_python(info) or Path(info["prefix"]).resolve() != folder.resolve():
                raise SetupError("New venv verification failed; no packages were installed")
        else:
            say("Skip venv: existing Python environment is usable")
        # Detect whether any package mutation is needed before requiring an app exit.
        probe = json.loads(capture([str(python), "-I", "-c", DEPENDENCY_PROBE, str(repo / "requirements.txt")], 120))
        if probe["missing"] or probe["conflicts"]:
            wait_for_apps(repo)
        if sys.platform.startswith("linux"):
            libs = {"libxcb-cursor0": "libxcb-cursor.so.0", "libegl1": "libEGL.so.1", "libgl1": "libGL.so.1",
                    "libxkbcommon0": "libxkbcommon.so.0", "libxkbcommon-x11-0": "libxkbcommon-x11.so.0",
                    "libxcb-icccm4": "libxcb-icccm.so.4", "libxcb-image0": "libxcb-image.so.0",
                    "libxcb-keysyms1": "libxcb-keysyms.so.1", "libxcb-render-util0": "libxcb-render-util.so.0",
                    "libxcb-xinerama0": "libxcb-xinerama.so.0"}
            missing = []
            for package, library in libs.items():
                try:
                    ctypes.CDLL(library)
                except OSError:
                    missing.append(package)
            if missing:
                with mutation_lock("OS-toolchain", 600):
                    apt_install(missing)
        installed = ensure_dependencies(repo, python)
        info.update(api=API, git=git, bash=bash, target=str(repo), commit=head, dependencies=installed)
        write_json(runtime, info)
        write_json(state_root() / "selection.json", {"target": str(repo), "api": API})
        write_json(repo / "data/setup/venv-owner.json", {"path": str(repo / ".venv"), "incomplete": False})
        say(f"Setup complete: {repo}")
        return info


def apply_runtime(repo):
    """Apply paths to this process only; no persistent PATH or registry changes."""
    info = read_json(Path(repo) / "data/setup/runtime.json")
    git, bash = info.get("git"), info.get("bash")
    if not git or not Path(git).is_file() or (sys.platform == "win32" and (not bash or not Path(bash).is_file())):
        git, bash = find_git()
    prepend = [str(venv_python(repo).parent)] if venv_python(repo).exists() else []
    if git:
        prepend.append(str(Path(git).parent))
    if venv_python(repo).exists():
        os.environ["VIRTUAL_ENV"] = str(Path(repo) / ".venv")
    os.environ["PATH"] = os.pathsep.join([*prepend, os.environ.get("PATH", "")])
    os.environ.setdefault("PYTHONPYCACHEPREFIX", str(Path(repo) / "data/pycache"))
    if bash:
        os.environ.setdefault("CLAUDE_CODE_GIT_BASH_PATH", bash)
    return info


def register_app(repo):
    """Called before Qt imports; retained until process exit, including restart."""
    import atexit
    folder = app_records(repo)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{os.getpid()}.json"
    with mutation_lock(repo, 10):
        write_json(path, {"pid": os.getpid(), "stamp": process_stamp(os.getpid())})
    atexit.register(lambda: path.unlink(missing_ok=True))


def launch(repo, extra=()):
    sys.path.insert(0, str(repo))
    from common.env import load_env
    load_env(Path(repo) / ".env")
    apply_runtime(repo)
    python = venv_python(repo)
    if not python.is_file():
        raise SetupError("Missing venv. Run setup again.")
    token = uuid.uuid4().hex
    with mutation_lock(repo):
        already_running = any(alive(read_json(p)) for p in app_records(repo).glob("*.json"))
        proc = None
        if not already_running:
            proc = subprocess.Popen([str(python), str(Path(repo) / "tool.py"), *extra], cwd=repo,
                                    env=dict(os.environ, MYANALYSIS_STARTUP_TOKEN=token), **quiet())
            # Register before releasing the lock, including legacy tool.py.
            write_json(app_records(repo) / f"{proc.pid}.json", {"pid": proc.pid, "stamp": process_stamp(proc.pid)})
    sys.path.insert(0, str(repo))
    from llm_bridge.commands import submit, wait_for
    deadline = time.monotonic() + 120
    result = None
    while (proc is None or proc.poll() is None) and time.monotonic() < deadline:
        # The bridge intentionally drops commands older than watcher startup.
        # Retry with a fresh id until the event loop has actually attached.
        request = submit("window", None, "list-open-datasets", {"setup_token": token} if proc is not None and (Path(repo) / "installer/api.json").is_file() else {})
        result = wait_for(request, timeout=2)
        if result and result.get("status") == "ok":
            break
        if result:
            time.sleep(.5)
    if not result or result.get("status") != "ok" or (proc is not None and proc.poll() is not None):
        raise SetupError("GUI readiness response not received. See terminal and data/logs.")
    say("MyAnalysis is already running; existing GUI responded." if already_running else "MyAnalysis GUI is ready.")
    return proc


def wait_for_gui(repo, proc):
    """Keep the Unix terminal open across an app-managed restart."""
    code = proc.wait()
    while any(alive(read_json(p)) for p in app_records(repo).glob("*.json")):
        time.sleep(.5)
    return code


def worker(args):
    result_path = Path(args.result)
    if sys.platform != "win32":
        # Keep the controlling terminal for sudo authentication, while monitor
        # closure and Ctrl+C cannot terminate the worker or an OS package manager.
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    os.environ["MYANALYSIS_SETUP_RESULT"] = str(result_path)
    try:
        repo = Path(args.install_dir)
        # Clone uses only the bundled core. Afterwards the checkout owns the
        # implementation; old checkouts without a core keep this compatibility copy.
        with mutation_lock(repo):
            check_children()
            if not find_git()[0]:
                wait_for_apps(repo)
            git, _ = ensure_git()
            install_checkout(repo, git)
        checkout_core = repo / "installer/core.py"
        if checkout_core.is_file() and checkout_core.resolve() != Path(__file__).resolve():
            minimum = read_json(repo / "installer/api.json").get("minimum", 1)
            if minimum > API:
                raise SetupError("Download the latest bootstrap from Releases; bootstrap API is incompatible.")
            # Windows execv starts a different OS process; that would make the
            # monitor observe its original worker exit before a result exists.
            import runpy
            sys.argv = [str(checkout_core), "--worker", "--install-dir", str(repo), "--result", str(result_path)]
            runpy.run_path(str(checkout_core), run_name="__main__")
        setup(repo)
        write_json(result_path, {"ok": True, "target": str(repo)})
    except Exception as exc:
        say(f"Setup incomplete: {exc}")
        write_json(result_path, {"ok": False, "error": str(exc)})
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="MyAnalysis setup (official Python; existing environments reused)")
    parser.add_argument("--install-dir")
    parser.add_argument("--source-dir")
    parser.add_argument("--no-pause", action="store_true")
    parser.add_argument("--no-launch", action="store_true")
    parser.add_argument("--launch", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--result", help=argparse.SUPPRESS)
    args, extra = parser.parse_known_args(argv)
    if args.worker:
        return worker(args)
    if extra and not args.launch:
        parser.error("unrecognized arguments: " + " ".join(extra))
    try:
        if not supported_python(python_info(sys.executable)):
            raise SetupError("Python 3.11+ 64-bit CPython is required (Windows: python.org, no Store/free-threaded build).")
        repo = select_target(args.install_dir, args.source_dir)
        if args.launch:
            proc = launch(repo, extra)
            return wait_for_gui(repo, proc) if proc is not None and sys.platform != "win32" else 0
        root = state_root() / "runs"
        root.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        log, result_path = root / (token + ".log"), root / (token + ".json")
        # Freeze the worker's source: an in-app update must not change it mid-run.
        core = root / (token + ".py")
        shutil.copyfile(__file__, core)
        with log.open("wb") as output:
            worker_env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            if sys.platform != "win32" and sys.stdin and sys.stdin.isatty():
                worker_env["MYANALYSIS_SETUP_TTY"] = os.ttyname(sys.stdin.fileno())
            proc = subprocess.Popen([sys.executable, "-u", str(core), "--worker", "--install-dir", str(repo), "--result", str(result_path)],
                                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                    env=worker_env, **(detached() if sys.platform == "win32" else {}))
        say(f"Setup log: {log}\nCtrl+C closes this monitor. Active OS installers continue; rerun to resume.")
        with log.open("r", encoding="utf-8", errors="replace") as reader:
            last = start = time.monotonic()
            while proc.poll() is None:
                text = reader.read(65536)
                if text:
                    say(text.rstrip())
                if time.monotonic() - last >= 10:
                    say(f"Setup running ({int(time.monotonic()-start)}s); log: {log}")
                    last = time.monotonic()
                pending = [read_json(p) for p in (state_root() / "children").glob("*.json")]
                if any(r.get("run_result") == str(result_path) and r.get("monitor_expired") for r in pending):
                    raise SetupError(f"OS installation is still running after 30 minutes. It continues under supervision. Finish its dialog, then rerun. Log: {log}")
                auth = result_path.with_suffix(".auth.json")
                answer = auth.with_suffix(".answer.json")
                auth_request = read_json(auth)
                if auth_request and auth_request.get("id") != read_json(answer).get("id"):
                    say("Authenticate to install missing OS packages: " + ", ".join(auth_request.get("packages", [])))
                    try:
                        authenticated = subprocess.run(["sudo", "-v"], timeout=600).returncode == 0
                    except KeyboardInterrupt:
                        write_json(answer, {"id": auth_request["id"], "ok": False})
                        raise
                    except subprocess.TimeoutExpired as exc:
                        write_json(answer, {"id": auth_request["id"], "ok": False})
                        raise SetupError("OS authentication timed out; no package installation was started") from exc
                    write_json(answer, {"id": auth_request["id"], "ok": authenticated})
                time.sleep(.2)
            say(reader.read().rstrip())
        result = read_json(result_path)
        if not result.get("ok"):
            raise SetupError(result.get("error", "Worker ended without a success result") + f"\nLog: {log}\nRerun the same setup to resume.")
        create_shortcut(repo)
        if not args.no_launch:
            proc = launch(repo)
            if proc is not None and sys.platform != "win32":
                return wait_for_gui(repo, proc)
        return 0
    except KeyboardInterrupt:
        say("Monitor closed. Wait for any OS installer to finish, then rerun setup.")
        return 130
    except (SetupError, OSError, ValueError) as exc:
        say(f"Setup incomplete: {exc}")
        if args.launch and sys.platform == "win32":
            from common.crashlog import show_fatal
            show_fatal("MyAnalysis", str(exc))
        if not args.no_pause and sys.stdin and sys.stdin.isatty():
            with contextlib.suppress(EOFError, KeyboardInterrupt):
                input("Press Enter to close. / Enter キーで閉じます。")
        return 1


def create_shortcut(repo):
    command = [str(venv_python(repo)), "-m", "installer.core"] if (Path(repo) / "installer/core.py").is_file() else [sys.executable, str(Path(__file__).resolve())]
    if sys.platform == "win32":
        # Pass all paths through env, never interpolate them into executable code.
        script = "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:MYANALYSIS_LINK);$s.TargetPath=$env:MYANALYSIS_PY;$s.Arguments=$env:MYANALYSIS_ARGS;$s.WorkingDirectory=$env:MYANALYSIS_TARGET;$s.Save()"
        link = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/MyAnalysis.lnk"
        link.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, MYANALYSIS_LINK=str(link), MYANALYSIS_PY=command[0], MYANALYSIS_TARGET=str(repo),
                   MYANALYSIS_ARGS=subprocess.list2cmdline([*command[1:], "--launch", "--install-dir", str(repo)]))
        capture(["powershell.exe", "-NoProfile", "-Command", script], env=env)
    elif sys.platform.startswith("linux"):
        desktop = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "applications/myanalysis.desktop"
        desktop.parent.mkdir(parents=True, exist_ok=True)
        # Desktop Exec has its own escaping, not shell syntax.
        def quote(value):
            return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%") + '"'
        desktop.write_text("[Desktop Entry]\nType=Application\nName=MyAnalysis\nTerminal=true\nExec=" + " ".join(quote(arg) for arg in [*command, "--launch", "--install-dir", str(repo)]) + "\nPath=" + str(repo) + "\nCategories=Science;\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
