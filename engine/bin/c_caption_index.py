#!/usr/bin/env python
"""c_caption_index.py -- Experiment E1, 2026-09-15.

Builds OUT/captions_fts.db: one FTS5 row per harvested table caption, so the
caption line can be searched at corpus scale as its own retrieval channel.

shelf.db is opened read-only and is NOT modified -- the shelf is a frozen
instrument and the caption index is a separate artefact beside it.

  python -u corpus-lab/bin/c_caption_index.py
"""
import json
import sqlite3
import pathlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import labpaths as L  # noqa: E402


def main():
    t0 = time.time()
    # Phase 9.2.2: --out-dir so the v2 shelf (and a portable folder's shelf) can
    # have its own caption artefacts. Default preserves behaviour exactly.
    import argparse
    _ap = argparse.ArgumentParser()
    _ap.add_argument("--out-dir", dest="out_dir", default=str(L.STACKS / "s7_shelf"))
    _a = _ap.parse_args()
    out_dir = pathlib.Path(_a.out_dir)
    src = sqlite3.connect(f"file:{out_dir / 'shelf.db'}?mode=ro", uri=True)
    dest_path = out_dir / "captions_fts.db"
    if dest_path.exists():
        dest_path.unlink()
    dest = sqlite3.connect(str(dest_path))
    dest.execute("CREATE VIRTUAL TABLE cap USING fts5("
                 "rel UNINDEXED, page_index UNINDEXED, caption)")

    n = 0
    batch = []
    for rel, page_index, caption in src.execute(
            "SELECT rel, page_index, caption FROM captions ORDER BY rowid"):
        batch.append((rel, page_index, caption or ""))
        if len(batch) >= 5000:
            dest.executemany("INSERT INTO cap VALUES (?,?,?)", batch)
            n += len(batch)
            batch = []
    if batch:
        dest.executemany("INSERT INTO cap VALUES (?,?,?)", batch)
        n += len(batch)
    dest.commit()
    dest.execute("INSERT INTO cap(cap) VALUES('optimize')")
    dest.commit()

    n_src = src.execute("SELECT COUNT(*) FROM captions").fetchone()[0]
    n_dest = dest.execute("SELECT COUNT(*) FROM cap").fetchone()[0]
    dest.close()
    src.close()

    rec = {"n_captions_source": n_src, "n_rows_indexed": n_dest,
           "rows_match": n_src == n_dest,
           "db_bytes": dest_path.stat().st_size,
           "wall_s": round(time.time() - t0, 1)}
    (L.STATE / "c_caption_index.json").write_text(
        json.dumps(rec, indent=1), encoding="utf-8")
    print(json.dumps(rec, indent=1))
    if not rec["rows_match"]:
        raise SystemExit("ROW_COUNT_MISMATCH")
    return 0


if __name__ == "__main__":
    sys.exit(main())
