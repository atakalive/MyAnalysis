"""Detached mutation workers for the existing updater and optional pip UI."""
from __future__ import annotations

import argparse
import dataclasses
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from installer import core


def monitor(kind, repo, *, progress=None, cancelled=None, **options):
    root = core.state_root() / "tasks" / uuid.uuid4().hex
    root.mkdir(parents=True)
    request, result, log = root / "request.json", root / "result.json", root / "output.log"
    core.write_json(request, dict(kind=kind, repo=str(repo), result=str(result), **options))
    python = Path(sys.executable)
    if python.name.lower() == "pythonw.exe":
        python = python.with_name("python.exe")
    with log.open("wb") as output:
        proc = subprocess.Popen([str(python), "-u", "-m", "installer.tasks", str(request)], cwd=repo,
                                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"), **core.detached())
    with log.open("r", encoding="utf-8", errors="replace") as reader:
        while proc.poll() is None:
            if cancelled and cancelled():
                (root / "cancel").touch()
            text = reader.read(65536)
            if text and progress:
                progress(text.rstrip())
            time.sleep(.1)
        text = reader.read(1024 * 1024)
        if text and progress:
            progress(text.rstrip())
    value = core.read_json(result)
    if not value:
        raise core.SetupError(f"Worker ended without a result; inspect {log}. Active children are checked before the next mutation.")
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path)
    args = parser.parse_args()
    request = core.read_json(args.request)
    repo = Path(request["repo"])
    try:
        with core.mutation_lock(repo):
            core.check_children()
            os.environ["MYANALYSIS_MUTATION_WORKER"] = "1"
            if request["kind"] == "update":
                from common import self_update
                result = self_update._run_update(repo, request.get("running"), request["url"], request["branch"], lambda stage: print(stage, flush=True))
            else:
                from common import embed_model
                result = embed_model._install_packages(lambda line: print(line, flush=True),
                                                       lambda: (args.request.parent / "cancel").exists())
        core.write_json(request["result"], dataclasses.asdict(result))
    except Exception as exc:
        if request["kind"] == "update":
            result = {"status": "failed", "stage": "fetch", "output": str(exc)}
        else:
            result = {"ok": False, "error": "pip", "message": str(exc)}
        core.write_json(request["result"], result)


if __name__ == "__main__":
    main()
