"""matplotlib figure primitives. GUI-free; used for batch PNG output."""
from pathlib import Path
from typing import Sequence
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.figure import Figure
import pandas as pd
import numpy as np


def trajectory(
    df: pd.DataFrame,
    x: str,
    y: str,
    color_by: str | None = None,
    highlights: Sequence[int] | None = None,
    vlines: Sequence[float] | None = None,
    title: str | None = None,
) -> Figure:
    """Scatter trajectory plot with optional category coloring and highlights.

    - color_by: column name. If given, points are colored by this categorical column.
      Rows with NaN in color_by are drawn as a separate "NaN" category (not dropped).
    - highlights: non-negative positional (iloc) row indices to circle.
      These are integer positions, not pandas index labels. Use df.index.get_loc()
      or integer arithmetic, not df.idxmin() on non-RangeIndex DataFrames.
      Negative indices raise ValueError.
    - vlines: x positions to draw vertical reference lines (e.g. phase boundaries).

    Raises ValueError if df is empty or highlights contains negative values.
    """
    if df.empty:
        raise ValueError("df must not be empty")
    fig, ax = plt.subplots(figsize=(7, 4))
    if color_by is not None:
        for cat, sub in df.groupby(color_by, sort=False, dropna=False):
            ax.scatter(sub[x], sub[y], label=str(cat), s=20)
        ax.legend(title=color_by, loc="best", fontsize=8)
    else:
        ax.scatter(df[x], df[y], s=20)
    if highlights is not None:
        if any(h < 0 for h in highlights):
            raise ValueError("highlights must be non-negative positional indices")
        ax.scatter(df[x].iloc[list(highlights)],
                   df[y].iloc[list(highlights)],
                   s=80, facecolors="none", edgecolors="red", linewidths=1.5,
                   label="highlight")
    if vlines is not None:
        for v in vlines:
            ax.axvline(v, color="0.7", linestyle="--", linewidth=0.8)
    ax.set_xlabel(x)
    ax.set_ylabel(y)
    if title:
        ax.set_title(title)
    fig.tight_layout()
    return fig


def image_grid(
    image_paths: Sequence[Path | str],
    titles: Sequence[str],
    ncols: int = 2,
    cmap: str = "viridis",
) -> Figure:
    """Arrange images in a grid. Loads via PIL → np.ndarray.

    Raises ValueError if image_paths is empty, ncols < 1,
    or len(image_paths) != len(titles).
    """
    if not image_paths:
        raise ValueError("image_paths must not be empty")
    if len(image_paths) != len(titles):
        raise ValueError(
            f"len(image_paths)={len(image_paths)} != len(titles)={len(titles)}")
    if ncols < 1:
        raise ValueError(f"ncols must be >= 1, got {ncols}")
    from PIL import Image
    n = len(image_paths)
    nrows = (n + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols, figsize=(4 * ncols, 4 * nrows), squeeze=False)
    for i, (p, t) in enumerate(zip(image_paths, titles)):
        ax = axes[i // ncols][i % ncols]
        with Image.open(p) as img:
            arr = np.array(img)
        ax.imshow(arr, cmap=cmap if arr.ndim == 2 else None)
        ax.set_title(t, fontsize=9)
        ax.axis("off")
    # blank out unused cells
    for j in range(n, nrows * ncols):
        axes[j // ncols][j % ncols].axis("off")
    fig.tight_layout()
    return fig


def overlay(
    dfs: Sequence[pd.DataFrame],
    x: str,
    y: str,
    vlines: Sequence[float] | None = None,
    alpha: float = 0.4,
    title: str | None = None,
) -> Figure:
    """Overlay y-vs-x line plots from multiple DataFrames (thin, semi-transparent).

    Raises ValueError if dfs is empty.
    """
    if not dfs:
        raise ValueError("dfs must not be empty")
    fig, ax = plt.subplots(figsize=(8, 4))
    for df in dfs:
        ax.plot(df[x], df[y], alpha=alpha, linewidth=0.8)
    if vlines is not None:
        for v in vlines:
            ax.axvline(v, color="0.5", linestyle="--", linewidth=0.8)
    ax.set_xlabel(x)
    ax.set_ylabel(y)
    if title:
        ax.set_title(title)
    fig.tight_layout()
    return fig


def bar_sorted(
    values: Sequence[float],
    labels: Sequence[str],
    ref: Sequence[float] | None = None,
    ylabel: str = "",
    title: str | None = None,
) -> Figure:
    """Bar chart sorted by `values` ascending. Optional reference values overlaid as dots.

    Raises ValueError if values is empty, len(values) != len(labels),
    ref is given and len(ref) != len(values), or any value/ref is non-finite.
    """
    if not values:
        raise ValueError("values must not be empty")
    if len(values) != len(labels):
        raise ValueError(
            f"len(values)={len(values)} != len(labels)={len(labels)}")
    if ref is not None and len(ref) != len(values):
        raise ValueError(
            f"len(ref)={len(ref)} != len(values)={len(values)}")
    if not all(np.isfinite(v) for v in values):
        raise ValueError("values must all be finite (no NaN or inf)")
    if ref is not None and not all(np.isfinite(r) for r in ref):
        raise ValueError("ref values must all be finite (no NaN or inf)")
    pairs = sorted(zip(values, labels, ref if ref is not None else [None] * len(values)),
                   key=lambda t: t[0])
    vs = [p[0] for p in pairs]
    ls = [p[1] for p in pairs]
    rs = [p[2] for p in pairs]
    fig, ax = plt.subplots(figsize=(max(6, 0.3 * len(vs)), 4))
    ax.bar(range(len(vs)), vs)
    if ref is not None:
        ax.scatter(range(len(vs)), rs, color="red", s=20, zorder=3, label="ref")
        ax.legend(fontsize=8)
    ax.set_xticks(range(len(vs)))
    ax.set_xticklabels(ls, rotation=90, fontsize=7)
    ax.set_ylabel(ylabel)
    if title:
        ax.set_title(title)
    fig.tight_layout()
    return fig


def save(fig: Figure, path: Path | str) -> None:
    """Save fig to path (parent dirs created) and close it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)
