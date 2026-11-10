"""PC-local backend commands. Discovery never downloads or changes settings."""
from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from pathlib import Path

ROOT_ENV = "MYANALYSIS_BACKENDS_DIR"
ENGINES = ("claude", "codex", "pi")


def install_root() -> Path:
    """The override is useful for isolated installation tests and portable setups."""
    override = os.environ.get(ROOT_ENV, "").strip()
    return Path(override) if override else Path.home() / ".myanalysis" / "backends"


def engine_key(engine_id: str) -> str:
    return "claude" if engine_id.startswith("claude") else engine_id


def host_target() -> str | None:
    os_name = {"win32": "win32", "darwin": "darwin", "linux": "linux"}.get(sys.platform)
    arch = {"amd64": "x64", "x86_64": "x64", "x64": "x64",
            "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine().lower())
    if not os_name or not arch:
        return None
    libc_name = platform.libc_ver()[0] if os_name == "linux" else ""
    musl = libc_name == "musl" or (
        os_name == "linux" and not libc_name and any(Path("/lib").glob("ld-musl-*.so.1")))
    suffix = "-musl" if musl else ""
    return f"{os_name}-{arch}{suffix}"


def supported(engine_id: str) -> bool:
    target = host_target()
    return (engine_key(engine_id) in ENGINES and target is not None
            and not (engine_key(engine_id) == "pi" and target.endswith("-musl")))


def engine_dir(engine_id: str) -> Path:
    target = host_target()
    key = engine_key(engine_id)
    if key not in ENGINES or target is None:
        raise ValueError(f"Unsupported backend/platform: {engine_id} / {target}")
    return install_root() / key / target


def _under(root: Path, value: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("Invalid managed command path")
    parts = value.split("/")
    if any(p in ("", ".", "..") or ":" in p for p in parts):
        raise ValueError("Invalid managed command path")
    path = root.joinpath(*parts)
    path.resolve().relative_to(root.resolve())
    return path


def managed_command(engine_id: str) -> tuple[str, ...] | None:
    """Read only the committed release; incomplete downloads are invisible."""
    try:
        root = engine_dir(engine_id)
        data = json.loads((root / "current.json").read_text(encoding="utf-8"))
        release = _under(root, data["release"])
        if release.parent != root / "releases":
            return None
        raw = data["argv"]
        if not isinstance(raw, list) or len(raw) != (2 if engine_key(engine_id) == "pi" else 1):
            return None
        paths = tuple(_under(release, part) for part in raw)
        if not all(p.is_file() for p in paths):
            return None
        if not os.access(paths[0], os.X_OK):
            return None
        return tuple(str(p) for p in paths)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return None


def managed_binary(engine_id: str) -> str | None:
    argv = managed_command(engine_id)
    return argv[-1] if argv else None


def is_managed_path(path: str, engine_id: str) -> bool:
    try:
        Path(path).resolve().relative_to(engine_dir(engine_id).resolve() / "releases")
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def expand_command(argv: list[str] | tuple[str, ...]) -> list[str]:
    """pi's managed entry is JS, launched directly with its adjacent private Node."""
    result = list(argv)
    if not result or not isinstance(result[0], str):
        return result
    if (Path(result[0]).suffix.lower() in (".js", ".mjs", ".cjs")
            and is_managed_path(result[0], "pi")):
        # Derive from the immutable release, not current.json (updates can race).
        entry = Path(result[0])
        for parent in entry.parents:
            if parent.parent.name == "releases":
                node = parent / "runtime" / ("node.exe" if sys.platform == "win32" else "bin/node")
                if node.is_file():
                    return [str(node), *result]
                break
    return result


def environment(argv: list[str] | tuple[str, ...], base: dict | None = None) -> dict:
    env = dict(os.environ if base is None else base)
    if argv and is_managed_path(argv[0], "claude"):
        env["DISABLE_UPDATES"] = "1"
    if argv and is_managed_path(argv[0], "pi"):
        env.pop("NODE_OPTIONS", None)
        env.pop("NODE_PATH", None)
        expanded = expand_command(argv)
        if expanded:
            env["PATH"] = os.pathsep.join(filter(None, (str(Path(expanded[0]).parent), env.get("PATH", ""))))
    return env


def which_pi(value: str = "pi") -> str | None:
    value = str(value or "pi").strip()
    if value == "pi":
        found = managed_binary("pi")
        if found:
            return found
    if sys.platform == "win32":
        for suffix in (".exe", ".cmd", ".bat"):
            found = shutil.which(value + suffix)
            if found:
                return found
    return shutil.which(value)


def install_command(engine_id: str) -> tuple[str, ...] | None:
    if not supported(engine_id):
        return None
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        console = executable.with_name("python.exe")
        if console.is_file():
            executable = console
    return (str(executable), "-u", "-m", "llm_backend.install", engine_key(engine_id))
