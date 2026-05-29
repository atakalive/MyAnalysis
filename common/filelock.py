"""Cross-platform advisory exclusive file lock.

POSIX uses fcntl.flock (blocks indefinitely); Windows uses msvcrt.locking,
which times out after ~10s per attempt, so we retry up to 3 times (~30s total).

Asymmetry note: POSIX blocks forever, Windows caps at ~30s. Fine for this
project's short GUI+CLI mutual exclusion; revisit if long holds are ever needed.
"""

import contextlib
import sys
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


@contextlib.contextmanager
def exclusive_lock(path: Path | str):
    """Acquire an exclusive advisory lock on `path` for the with-block."""
    if sys.platform == "win32":
        lf = open(path, "w")
        try:
            last_err: OSError | None = None
            for _ in range(3):
                try:
                    msvcrt.locking(lf.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError as e:
                    last_err = e
            else:
                raise last_err
            try:
                yield
            finally:
                msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            lf.close()
    else:
        lf = open(path, "w")
        try:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
        finally:
            lf.close()
