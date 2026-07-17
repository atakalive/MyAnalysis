"""Tests for core.figures.save writing PNGs without touching os.path.realpath.

core.figures.save renders to a BytesIO and writes bytes via atomic_write_bytes,
so it must succeed even when os.path.realpath raises OSError [WinError 1005] like
it does on rclone/WinFsp mounts — and WITHOUT relying on the common.mount_compat
shim. (Plain fig.savefig(path) would fail there because matplotlib's PNG writer
goes through PIL.Image.save -> os.path.realpath.)
"""
from __future__ import annotations

import os

import pytest

matplotlib = pytest.importorskip("matplotlib")
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from core import figures  # noqa: E402

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def test_save_writes_png_when_output_path_is_on_failing_mount(monkeypatch, tmp_path):
    # Model the mount realistically: realpath raises 1005 ONLY for paths under the
    # mount (tmp_path); local paths (e.g. matplotlib's font files on the real disk,
    # which its font manager resolves via os.path.realpath) still work. save() must
    # not route the OUTPUT path through realpath — proving it independently of the
    # common.mount_compat shim.
    real = os.path.realpath
    mount = str(tmp_path)

    def mount_realpath(path, *, strict=False):
        if mount in os.fspath(path):
            raise OSError(1005, "The volume does not contain a recognized file system")
        return real(path, strict=strict)

    monkeypatch.setattr(os.path, "realpath", mount_realpath)
    # Sanity: the simulated mount really does break plain PIL-path access.
    with pytest.raises(OSError):
        os.path.realpath(tmp_path / "probe")

    fig, ax = plt.subplots()
    ax.plot([0, 1, 2], [0, 1, 4])
    out = tmp_path / "sub" / "fig.png"           # parent dirs are created by save()
    figures.save(fig, out)                        # must not raise
    data = out.read_bytes()
    assert data[:8] == _PNG_SIG                   # valid PNG
    assert list((tmp_path / "sub").glob("*.tmp")) == []   # atomic write left no tmp


def test_save_closes_figure(tmp_path):
    fig, ax = plt.subplots()
    ax.plot([0, 1], [1, 0])
    figures.save(fig, tmp_path / "f.png")
    assert not plt.fignum_exists(fig.number)      # save() closed the figure
