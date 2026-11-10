"""Download official backends into the application's private, versioned storage.

Run with ``python -m llm_backend.install claude|codex|pi``. No global installer,
registry edits, user npm configuration, authentication, or inference is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from common.filelock import try_exclusive_lock
from common.proc import kill_tree, no_window_kwargs
from llm_backend import runtime

CLAUDE_BASE = "https://downloads.claude.ai/claude-code-releases"
CODEX_API = "https://api.github.com/repos/openai/codex/releases/latest"
PI_BASE = "https://pi.dev/api/installer/releases"
NODE_BASE = "https://nodejs.org/dist/latest-v22.x"
_VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")
_CHUNK = 256 * 1024
_MAX_ARCHIVE = 2 * 1024**3
_MAX_EXTRACTED = 4 * 1024**3


class InstallError(RuntimeError):
    pass


def _open(url: str):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise InstallError("Only HTTPS downloads are supported")
    request = urllib.request.Request(url, headers={"User-Agent": "MyAnalysis-backend-installer"})
    response = urllib.request.urlopen(request, timeout=30)
    if urllib.parse.urlsplit(response.url).scheme != "https":
        response.close()
        raise InstallError("Download redirected away from HTTPS")
    return response


def _json(url: str) -> dict:
    with _open(url) as response:
        raw = response.read(16 * 1024**2 + 1)
    if len(raw) > 16 * 1024**2:
        raise InstallError("Release metadata is too large")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise InstallError("Invalid release metadata")
    return data


def _version(value: object) -> str:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise InstallError(f"Invalid release version: {value!r}")
    return value


def _check(cancelled) -> None:
    if cancelled():
        raise InstallError("Installation cancelled")


def _download(url: str, dest: Path, checksum: str, log, cancelled) -> None:
    algorithm, separator, expected = checksum.partition(":")
    if not separator or algorithm not in ("sha256", "sha512"):
        raise InstallError("Missing download integrity information")
    digest = hashlib.new(algorithm)
    size, reported = 0, 0
    log(f"Downloading {dest.name}")
    with _open(url) as response, dest.open("wb") as output:
        while True:
            _check(cancelled)
            chunk = response.read(_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_ARCHIVE:
                raise InstallError("Download exceeds size limit")
            output.write(chunk)
            digest.update(chunk)
            if size - reported >= 8 * 1024**2:
                log(f"{dest.name}: {size // 1024**2} MiB")
                reported = size
    if digest.hexdigest().lower() != expected.lower():
        raise InstallError(f"Checksum mismatch: {dest.name}")


def _member_path(root: Path, name: str) -> Path:
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or any(part == ".." or ":" in part for part in p.parts):
        raise InstallError(f"Unsafe archive path: {name}")
    result = root.joinpath(*p.parts)
    result.resolve().relative_to(root.resolve())
    return result


def _extract(archive: Path, dest: Path, cancelled) -> None:
    dest.mkdir(parents=True)
    total = 0
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                _check(cancelled)
                path = _member_path(dest, member.filename)
                total += member.file_size
                if total > _MAX_EXTRACTED or (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise InstallError("Unsupported archive size or symbolic link")
                if member.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as source, path.open("wb") as output:
                        shutil.copyfileobj(source, output, _CHUNK)
        return
    links = []
    with tarfile.open(archive, "r:*") as tf:
        for member in tf:
            _check(cancelled)
            path = _member_path(dest, member.name)
            total += member.size
            if total > _MAX_EXTRACTED:
                raise InstallError("Archive exceeds extracted size limit")
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                path.parent.mkdir(parents=True, exist_ok=True)
                with tf.extractfile(member) as source, path.open("wb") as output:
                    shutil.copyfileobj(source, output, _CHUNK)
                path.chmod(member.mode & 0o777)
            elif member.issym() or member.islnk():
                links.append((member, path))
            else:
                raise InstallError(f"Unsupported archive member: {member.name}")
        # Create links last: no extraction can subsequently follow an archive link.
        for member, path in links:
            _check(cancelled)
            link = PurePosixPath(member.linkname.replace("\\", "/"))
            if link.is_absolute() or any(":" in part for part in link.parts):
                raise InstallError("Unsafe archive link")
            target = (path.parent if member.issym() else dest).joinpath(*link.parts)
            target.resolve().relative_to(dest.resolve())
            path.parent.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                if not target.is_file():
                    raise InstallError("Archive link target is not a file")
                shutil.copy2(target, path)
            else:
                path.symlink_to(os.path.relpath(target, path.parent))


def _checksum_list(url: str, filename: str) -> str:
    with _open(url) as response:
        text = response.read(2 * 1024**2).decode("utf-8")
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == filename and re.fullmatch(r"[a-fA-F0-9]{64}", parts[0]):
            return "sha256:" + parts[0]
    raise InstallError(f"No checksum published for {filename}")


def _claude(stage: Path, target: str, log, cancelled) -> tuple[str, list[Path]]:
    with _open(CLAUDE_BASE + "/latest") as response:
        version = _version(response.read(128).decode("utf-8").strip())
    manifest = _json(f"{CLAUDE_BASE}/{version}/manifest.json")
    info = manifest.get("platforms", {}).get(target)
    if not isinstance(info, dict):
        raise InstallError(f"No Claude distribution for {target}")
    name = "claude.exe" if target.startswith("win32-") else "claude"
    dest = stage / name
    _download(f"{CLAUDE_BASE}/{version}/{target}/{name}", dest,
              "sha256:" + str(info.get("checksum", "")), log, cancelled)
    dest.chmod(0o755)
    return version, [dest]


def _codex(stage: Path, target: str, log, cancelled) -> tuple[str, list[Path]]:
    release = _json(CODEX_API)
    if release.get("prerelease") or release.get("draft"):
        raise InstallError("Codex release is not a stable release")
    tag = release.get("tag_name", "")
    version = _version(tag.removeprefix("rust-v"))
    os_name, arch, *_ = target.split("-")
    triple = {"win32": "pc-windows-msvc", "darwin": "apple-darwin", "linux": "unknown-linux-musl"}[os_name]
    cpu = "x86_64" if arch == "x64" else "aarch64"
    name = f"codex-package-{cpu}-{triple}.tar.gz"
    assets = {a["name"]: a for a in release.get("assets", [])}
    if name not in assets:
        raise InstallError(f"No Codex package for {target}")
    asset = assets[name]
    checksum = asset.get("digest") or ""
    if not checksum.startswith("sha256:"):
        sums = assets.get("codex-package_SHA256SUMS", {})
        checksum = _checksum_list(sums.get("browser_download_url", ""), name)
    archive = stage / "codex.tar.gz"
    _download(asset["browser_download_url"], archive, checksum, log, cancelled)
    log("Extracting Codex package")
    package = stage / "package"
    _extract(archive, package, cancelled)
    manifests = list(package.rglob("codex-package.json"))
    if len(manifests) != 1:
        raise InstallError("Codex standalone package manifest is missing")
    entry = manifests[0].parent / "bin" / ("codex.exe" if os_name == "win32" else "codex")
    if not entry.is_file():
        raise InstallError("Codex package entry point is missing")
    archive.unlink()
    return version, [entry]


def _npm_env(stage: Path, node: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.lower().startswith("npm_config_")}
    env.pop("NODE_OPTIONS", None)
    env.pop("NODE_PATH", None)
    config = stage / "npm-user.npmrc"
    global_config = stage / "npm-global.npmrc"
    config.write_text("", encoding="utf-8")
    global_config.write_text("", encoding="utf-8")
    env.update({"npm_config_userconfig": str(config), "npm_config_globalconfig": str(global_config),
                "npm_config_cache": str(stage / "npm-cache"), "npm_config_registry": "https://registry.npmjs.org",
                "npm_config_update_notifier": "false", "npm_config_global": "false"})
    env["PATH"] = os.pathsep.join(filter(None, (str(node.parent), env.get("PATH", ""))))
    return env


def _run(argv: list[str], cwd: Path, env: dict, log, cancelled, timeout=600) -> str:
    _check(cancelled)
    proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=(sys.platform != "win32"), **no_window_kwargs())
    output = []
    stop_reader = threading.Event()

    def read():
        for line in proc.stdout:
            text = line.decode("utf-8", "replace").rstrip()
            output.append(text)
            log(text)
        stop_reader.set()

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    try:
        while proc.poll() is None:
            _check(cancelled)
            if time.monotonic() > deadline:
                raise InstallError("Command timed out")
            stop_reader.wait(0.1)
        reader.join(5)
        if reader.is_alive():
            raise InstallError("Command left child processes running")
        if proc.returncode:
            raise InstallError(f"Command failed (exit {proc.returncode}): {Path(argv[0]).name}")
        return "\n".join(output)
    finally:
        if proc.poll() is None or reader.is_alive():
            kill_tree(proc)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        reader.join(2)
        if not reader.is_alive():
            proc.stdout.close()


def _pi(stage: Path, target: str, log, cancelled) -> tuple[str, list[Path]]:
    metadata = _json(PI_BASE + "/latest")
    version = _version(metadata.get("version"))
    os_name, arch = target.split("-")
    node_os = {"win32": "win", "darwin": "darwin", "linux": "linux"}[os_name]
    ext = "zip" if os_name == "win32" else "tar.gz"
    with _open(NODE_BASE + "/SHASUMS256.txt") as response:
        lines = response.read(1024**2).decode("utf-8").splitlines()
    candidates = [line.split() for line in lines if line.endswith(f"-{node_os}-{arch}.{ext}")]
    if len(candidates) != 1:
        raise InstallError(f"No private Node.js distribution for {target}")
    checksum, name = candidates[0]
    archive = stage / ("node." + ext)
    _download(NODE_BASE + "/" + name, archive, "sha256:" + checksum, log, cancelled)
    unpacked = stage / "node-unpacked"
    log("Extracting private Node.js runtime")
    _extract(archive, unpacked, cancelled)
    children = list(unpacked.iterdir())
    if len(children) != 1 or not children[0].is_dir():
        raise InstallError("Invalid Node.js archive layout")
    node_root = stage / "runtime"
    children[0].rename(node_root)
    unpacked.rmdir()
    archive.unlink()
    node = node_root / ("node.exe" if os_name == "win32" else "bin/node")
    npm = node_root / ("node_modules/npm/bin/npm-cli.js" if os_name == "win32" else "lib/node_modules/npm/bin/npm-cli.js")
    agent = stage / "agent"
    agent.mkdir()
    package = _json(f"{PI_BASE}/{version}/package.json")
    lock = _json(f"{PI_BASE}/{version}/package-lock.json")
    name = "@earendil-works/pi-coding-agent"
    locked = lock.get("packages", {})
    if (package.get("version") != version or package.get("dependencies", {}).get(name) != version
            or lock.get("version") != version or lock.get("lockfileVersion") != 3
            or locked.get("", {}).get("dependencies", {}).get(name) != version
            or locked.get("node_modules/" + name, {}).get("version") != version):
        raise InstallError("Pi release metadata and dependency lock do not match")
    integrities = {p.get("tarball"): p.get("integrity") for p in metadata.get("packages", [])}
    for entry in locked.values():
        url = entry.get("resolved", "")
        if url.startswith("https:") and not entry.get("integrity"):
            integrity = integrities.get(url)
            if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
                raise InstallError("Pi dependency has no download integrity")
            entry["integrity"] = integrity
    (agent / "package.json").write_text(json.dumps(package), encoding="utf-8")
    (agent / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    env = _npm_env(stage, node)
    log(f"Installing Pi {version} with pinned dependencies")
    _run([str(node), str(npm), "ci", "--ignore-scripts", "--omit=dev", "--include=optional",
          "--global=false", "--no-audit", "--no-fund", "--progress=false", "--prefix", str(agent)],
         agent, env, log, cancelled)
    installed = agent / "node_modules" / "@earendil-works" / "pi-coding-agent"
    pkg = json.loads((installed / "package.json").read_text(encoding="utf-8"))
    if pkg.get("version") != version:
        raise InstallError("Installed Pi version does not match its release")
    entry = runtime._under(installed, pkg["bin"]["pi"])
    return version, [node, entry]


def install(engine_id: str, *, log=print, cancelled=lambda: False) -> dict:
    key = runtime.engine_key(engine_id)
    if not runtime.supported(key):
        raise InstallError(f"No distribution for {key} / {runtime.host_target()}")
    root = runtime.engine_dir(key)
    root.mkdir(parents=True, exist_ok=True)
    with try_exclusive_lock(root / "install.lock") as acquired:
        if not acquired:
            raise InstallError("Another installation of this backend is running")
        stage = root / ("staging-" + uuid.uuid4().hex)
        stage.mkdir()
        try:
            _check(cancelled)
            version, argv = {"claude": _claude, "codex": _codex, "pi": _pi}[key](
                stage, runtime.host_target(), log, cancelled)
            if not all(p.is_file() for p in argv):
                raise InstallError("Installed command files are missing")
            log(f"Verifying {key} {version}")
            env = runtime.environment([str(p) for p in argv])
            if key == "claude":
                env["DISABLE_UPDATES"] = "1"
            elif key == "pi":
                # Staging is not a committed managed path yet.
                env.pop("NODE_OPTIONS", None)
                env.pop("NODE_PATH", None)
                env["PATH"] = os.pathsep.join(filter(None, (str(argv[0].parent), env.get("PATH", ""))))
            output = _run([*[str(p) for p in argv], "--version"], stage, env, log, cancelled, timeout=45)
            if not re.search(r"(?<![\d.])" + re.escape(version) + r"(?![\d.])", output):
                raise InstallError("Installed executable returned an unexpected version")
            _check(cancelled)
            relative = [p.relative_to(stage).as_posix() for p in argv]
            release_name = version + "-" + uuid.uuid4().hex[:12]
            releases = runtime._under(root, "releases")
            releases.mkdir(exist_ok=True)
            stage.rename(releases / release_name)
            data = {"version": version, "release": "releases/" + release_name, "argv": relative}
            pointer = root / ("current-" + uuid.uuid4().hex + ".tmp")
            try:
                with pointer.open("w", encoding="utf-8") as stream:
                    json.dump(data, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                _check(cancelled)
                os.replace(pointer, root / "current.json")
            finally:
                pointer.unlink(missing_ok=True)
            log(f"Installed {key} {version}")
            return data
        finally:
            # stage was created by this invocation; old releases are never removed.
            if stage.is_dir():
                if stage.is_symlink() or stage.resolve().parent != root.resolve():
                    raise InstallError("Refusing to clean a staging path outside the installation root")
                shutil.rmtree(stage)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engine", choices=("claude", "claude-cli", "claude-vscode", "codex", "pi"))
    args = parser.parse_args(argv)
    event = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: event.set())
    try:
        install(args.engine, log=lambda text: print(text, flush=True), cancelled=event.is_set)
    except Exception as error:
        print(f"[error] {type(error).__name__}: {error}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
