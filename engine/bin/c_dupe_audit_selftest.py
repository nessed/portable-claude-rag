#!/usr/bin/env python
"""c_dupe_audit_selftest.py -- does c_dupe_audit.py actually flag the bad keeper?

The audit returned zero groups on both corpora available here (this corpus has no
identical-content copies at all, and the 15,000-document fixture's planted copies land in
different families), so "it printed 0" proves nothing about whether it can detect a bad
keeper. This writes the two rows by hand -- a shelf and an index with a keeper and a
fatter shadow -- and asserts the verdict both ways.

    python engine/bin/c_dupe_audit_selftest.py     # exit 0 pass, 1 fail
"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import c_dupe_audit as DA  # noqa: E402


def make(tmp, keeper_pages, keeper_bytes, shadow_pages, shadow_bytes):
    Path(tmp).mkdir(parents=True, exist_ok=True)
    db = Path(tmp) / "pages.db"
    shelf = Path(tmp) / "shelf.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE files(rel TEXT, n_pages INTEGER, size INTEGER)")
    con.executemany("INSERT INTO files VALUES(?,?,?)", [
        ("series/complete 2025.pdf", keeper_pages, keeper_bytes),
        ("series/one-off draft 2025.pdf", shadow_pages, shadow_bytes)])
    con.commit(); con.close()
    con = sqlite3.connect(str(shelf))
    con.execute("CREATE TABLE docs(rel TEXT PRIMARY KEY, family TEXT, fy_primary TEXT, "
                "n_pages INTEGER, size INTEGER, dupes TEXT, n_dupes INTEGER)")
    con.execute("INSERT INTO docs VALUES(?,?,?,?,?,?,?)",
                ("series/complete 2025.pdf", "series", "2025", keeper_pages, keeper_bytes,
                 json.dumps(["series/one-off draft 2025.pdf"]), 1))
    con.commit(); con.close()
    return db, shelf


def main():
    checks = []
    with tempfile.TemporaryDirectory() as td:
        # the shadow is fatter: pages first, so 150 pages beats 120 even with fewer bytes
        db, shelf = make(Path(td) / "risky", 120, 9_000_000, 150, 8_000_000)
        res = DA.audit(db, shelf)
        checks.append(("counts_the_group", res["groups_with_copies"] == 1))
        checks.append(("flags_the_fatter_shadow", res["risky_groups"] == 1))
        checks.append(("pages_beat_bytes",
                       res["groups"][0]["shadows"][0]["beats_keeper"] is True))

        # a bigger-on-disk but shorter re-scan is NOT more complete: a citation needs pages
        db, shelf = make(Path(td) / "bigbytes", 120, 4_000_000, 118, 9_000_000)
        res = DA.audit(db, shelf)
        checks.append(("fewer_pages_is_not_better", res["risky_groups"] == 0))

        # and the honest case: the keeper is the complete file
        db, shelf = make(Path(td) / "fine", 400, 9_000_000, 12, 400_000)
        res = DA.audit(db, shelf)
        checks.append(("complete_keeper_passes", res["risky_groups"] == 0))
        checks.append(("still_reports_the_group", res["groups_with_copies"] == 1))

    n_pass = sum(1 for _, ok in checks if ok)
    for name, ok in checks:
        print("%-32s %s" % (name, "pass" if ok else "FAIL"))
    print("dupe audit: %d/%d pass" % (n_pass, len(checks)))
    return 0 if n_pass == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
