"""Generic data loaders."""
import sys
from pathlib import Path
import pandas as pd


def load_csv_per_subdir(
    root: Path,
    subdir_pattern: str,
    csv_name: str = "samples.csv",
    encoding: str | None = None,
) -> list[dict]:
    """Load `<csv_name>` from each subdir of `root` matching `subdir_pattern`.

    Returns a list of dicts (one per subdir, sorted by name), each with:
        - "name": subdir name (str)
        - "dir":  subdir absolute path (Path)
        - "df":   the loaded DataFrame

    Raises FileNotFoundError if `root` doesn't exist.
    Subdirs without `<csv_name>` are skipped with a stderr warning.
    Subdirs where `<csv_name>` fails to parse are skipped with a stderr warning.
    Returns an empty list (with a stderr warning) if no valid data is loaded.

    Parameters
    ----------
    encoding : str or None
        Passed to pd.read_csv(). None uses pandas default (utf-8).
        For Shift-JIS data, pass encoding="cp932".
    """
    root = Path(root).resolve()
    if not root.exists():
        raise FileNotFoundError(f"root not found: {root}")
    out: list[dict] = []
    for d in sorted(root.glob(subdir_pattern)):
        if not d.is_dir():
            continue
        csv = d / csv_name
        if not csv.exists():
            print(f"warning: {csv} not found, skipping", file=sys.stderr)
            continue
        try:
            df = pd.read_csv(csv, encoding=encoding)
        except (pd.errors.ParserError, UnicodeDecodeError, ValueError) as e:
            print(f"warning: failed to parse {csv}: {e}", file=sys.stderr)
            continue
        out.append({"name": d.name, "dir": d, "df": df})
    if not out:
        print(
            f"warning: no valid data loaded from {root} "
            f"(pattern={subdir_pattern!r}, csv={csv_name!r})",
            file=sys.stderr,
        )
    return out
