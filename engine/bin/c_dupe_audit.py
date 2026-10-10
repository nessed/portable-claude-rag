#!/usr/bin/env python
"""c_dupe_audit.py -- is the copy the shelf kept the one you would have chosen?

A shelf keeps ONE document per edition group and records the identical-content copies in
that document's `dupes` list. Which one survives is a key ordering, and the key is not
completeness: the author's own v2 shelf notes record a one-off draft winning over the
complete file in 153 clusters. Nothing in the v1 path reports that, so a corpus with
re-uploaded copies can cite the thinner document and never say so.

This prints every edition group that HAS copies, the keeper and its shadows, and flags the
groups where a shadow carries more pages than the keeper -- the case where "the largest
copy is primary" is not what happened.

  python engine/bin/c_dupe_audit.py --db engine/artefacts-research/pages.db \
      --shelf engine/artefacts-research/shelf/shelf.db [--top 20] [--json]

Exit 0 when no shadow beats its keeper, 1 when some do (so a build can gate on it).
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path


def _ro(p):
    con = sqlite3.connect("file:%s?mode=ro" % Path(p).as_posix(), uri=True)
    con.row_factory = sqlite3.Row
    return con


def audit(db_path, shelf_path, top=None):
    shelf = _ro(shelf_path)
    db = _ro(db_path)
    sizes = {r["rel"]: (r["n_pages"], r["size"]) for r in
             db.execute("SELECT rel, n_pages, size FROM files")}
    groups = []
    for row in shelf.execute(
            "SELECT rel, family, fy_primary, n_pages, size, dupes FROM docs "
            "WHERE n_dupes > 0 ORDER BY n_dupes DESC, family"):
        keeper_pages, keeper_size = sizes.get(row["rel"], (row["n_pages"], row["size"]))
        shadows = []
        for rel in json.loads(row["dupes"] or "[]"):
            pages, size = sizes.get(rel, (None, None))
            shadows.append({
                "rel": rel, "n_pages": pages, "size": size,
                # "beats" is about pages first, bytes second: a re-scan can be bigger on
                # disk while holding fewer pages, and pages are what a citation needs.
                "beats_keeper": bool(
                    pages is not None and keeper_pages is not None
                    and (pages, size or 0) > (keeper_pages, keeper_size or 0)),
            })
        groups.append({
            "family": row["family"], "fy": row["fy_primary"], "keeper": row["rel"],
            "keeper_pages": keeper_pages, "keeper_size": keeper_size,
            "shadows": shadows,
            "risky": any(s["beats_keeper"] for s in shadows),
        })
    shelf.close()
    db.close()
    groups.sort(key=lambda g: (-int(g["risky"]), -len(g["shadows"])))
    return {
        "groups_with_copies": len(groups),
        "copies_hidden": sum(len(g["shadows"]) for g in groups),
        "risky_groups": sum(1 for g in groups if g["risky"]),
        "groups": groups[:top] if top else groups,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="the corpus's pages.db")
    ap.add_argument("--shelf", required=True, help="the shelf's shelf.db")
    ap.add_argument("--top", type=int, default=None, help="print only the worst N groups")
    ap.add_argument("--json", dest="as_json", action="store_true")
    a = ap.parse_args()
    res = audit(a.db, a.shelf, a.top)
    if a.as_json:
        print(json.dumps(res, indent=1))
        return 1 if res["risky_groups"] else 0

    print("DUPE_AUDIT groups_with_copies=%d copies_hidden=%d risky=%d"
          % (res["groups_with_copies"], res["copies_hidden"], res["risky_groups"]))
    for g in res["groups"]:
        mark = "RISKY" if g["risky"] else "ok   "
        print("  [%s] %s / %s" % (mark, g["family"][:44], g["fy"] or "-"))
        print("      keeper %s  (%s pages, %s bytes)"
              % (g["keeper"][-58:], g["keeper_pages"], g["keeper_size"]))
        for s in g["shadows"]:
            print("      shadow %s  (%s pages, %s bytes)%s"
                  % (s["rel"][-58:], s["n_pages"], s["size"],
                     "  <- MORE COMPLETE THAN THE KEEPER" if s["beats_keeper"] else ""))
    if res["risky_groups"]:
        print("DUPE_AUDIT_RISKY -- %d group(s) kept a document that a hidden copy beats. "
              "Open the keeper and check it is the complete file before citing it."
              % res["risky_groups"])
    return 1 if res["risky_groups"] else 0


if __name__ == "__main__":
    sys.exit(main())
