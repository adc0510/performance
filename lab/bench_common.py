"""Shared cleanup helper for the streaming benchmarks."""

import shutil
from pathlib import Path


def clean_pipeline_files(wal_path: Path, spill_dir: Path) -> None:
    """Removes every artifact a previous run of this benchmark could have
    left behind: all WAL segments (`<wal>.000000`, `<wal>.000001`, ...), the
    shipper's persisted offsets file, and the spill directory.
    """
    for p in wal_path.parent.glob(f"{wal_path.name}.*"):
        p.unlink()
    shutil.rmtree(spill_dir, ignore_errors=True)
