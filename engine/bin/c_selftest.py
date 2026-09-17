#!/usr/bin/env python
"""c_selftest.py - build C, Stage 2.3. Runs the Gate 2 self-test table from
plans_fable/C_SHELF_FIRST_BUILD.md and records STATE/c_shelf_selftest.json.
Run from cwd = RUNG (read-only use; nothing installed) per the spec.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import labpaths as L  # noqa: E402
import c_shelf as CSH  # noqa: E402

DB = str(L.STACKS / "s2_fts5" / "harness_15000.db")
SHELF = str(L.STACKS / "s7_shelf" / "shelf.db")
PY = sys.executable
SHELF_PY = str(Path(__file__).resolve().parent / "c_shelf.py")


def main():
    # Phase 9: --db/--shelf default to today's values, so every recorded run of
    # this file reproduces exactly; Phase 2.3 points them at the v2 shelf and
    # Phase 6.2 at a portable folder's own artefacts. --expect-pages likewise
    # keeps the 15,000-rung coverage assertion as the default.
    global DB, SHELF
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--shelf", default=SHELF)
    ap.add_argument("--expect-pages", dest="expect_pages", type=int, default=1206260,
                    help="0 = do not assert a page count (a folder other than the 15,000 rung)")
    ap.add_argument("--out", default=str(L.STATE / "c_shelf_selftest.json"))
    a = ap.parse_args()
    DB, SHELF = a.db, a.shelf

    results = {}
    ctx = CSH.get_ctx(DB, SHELF)

    # coverage
    cov = CSH.do_coverage(ctx)
    results["coverage"] = {
        "pass": (cov["addressable_pages"] == a.expect_pages if a.expect_pages
                 else cov["addressable_pages"] > 0),
        "pages": cov["addressable_pages"]}

    # find "budget" -- cold then warm
    t0 = time.time()
    res_cold = CSH.do_find(ctx, ["budget"])
    cold_s = time.time() - t0
    t0 = time.time()
    res_warm = CSH.do_find(ctx, ["budget"])
    warm_s = time.time() - t0
    results["find_budget"] = {
        "pass": len(res_warm["families"]) >= 3 and "receipt" in res_warm and "coverage" in res_warm,
        "n_families": len(res_warm["families"]), "cold_s": round(cold_s, 2),
        "warm_s": round(warm_s, 2), "warm_le_5s": warm_s <= 5.0,
    }

    # have "economic survey"
    res = CSH.do_have(ctx, "economic survey")
    best = res["families"][0] if res["families"] else None
    results["have_economic_survey"] = {
        "pass": bool(best and len(best["fy_list"]) >= 10),
        "best_family_n_editions": len(best["fy_list"]) if best else 0,
    }
    fy_list = best["fy_list"] if best else []

    # have --fy absent
    res = CSH.do_have(ctx, "economic survey", fy="2029-30")
    status = res.get("fy_check", {}).get("status")
    results["have_fy_absent"] = {"pass": status == "NO_EDITION_FOR", "status": status}

    # have --fy present
    present_fy = fy_list[0] if fy_list else None
    status_present = None
    if present_fy:
        res = CSH.do_have(ctx, "economic survey", fy=present_fy)
        status_present = res.get("fy_check", {}).get("status")
    results["have_fy_present"] = {"pass": status_present == "EDITION_PRESENT",
                                   "status": status_present, "fy_tested": present_fy}

    # have nonexistent
    res = CSH.do_have(ctx, "zqxv nonexistent publication")
    results["have_no_match"] = {"pass": res["no_family_matches"] is True}

    # pick "the largest family's latest edition" -- most members, RESTRICTED to
    # families whose primaries actually look like publications (avg >= 150
    # pages). Unrestricted this picks e.g. a 340-member CSV/data-header
    # cluster (avg 1 page) -- large in file count, not a navigable document.
    fam_rows = ctx.shelf.execute("""
        SELECT f.family, f.n_members
        FROM families f
        JOIN (SELECT family, AVG(n_pages) a FROM docs
              WHERE is_primary=1 AND fy_primary IS NOT NULL GROUP BY family) avgp
          ON avgp.family = f.family
        WHERE avgp.a >= 150
        ORDER BY f.n_members DESC LIMIT 1
    """).fetchone()
    target_rel = None
    if fam_rows:
        fam = fam_rows[0]
        primaries = sorted(
            [r for r in ctx.rel_to_row.values() if r["family"] == fam and r["is_primary"] and r["fy_primary"]],
            key=lambda r: r["fy_primary"])
        if primaries:
            target_rel = primaries[-1]["rel"]
        else:
            same = [r for r in ctx.rel_to_row.values() if r["family"] == fam and r["is_primary"]]
            target_rel = same[0]["rel"] if same else None

    # inside ... "contents"
    if target_rel:
        t0 = time.time()
        ins = CSH.do_inside(ctx, target_rel, "contents")
        ins_s = time.time() - t0
        results["inside_contents"] = {"pass": len(ins["hits"]) >= 1 and ins_s <= 2.0,
                                       "n_hits": len(ins["hits"]), "wall_s": round(ins_s, 2)}
    else:
        results["inside_contents"] = {"pass": False, "reason": "no target family found"}

    # tables
    if target_rel:
        tb = CSH.do_tables(ctx, target_rel)
        results["tables"] = {"pass": len(tb["captions"]) >= 20, "n_captions": len(tb["captions"])}
    else:
        results["tables"] = {"pass": False}

    # series "production" --family "economic survey"
    ser = CSH.do_series(ctx, "production", "economic survey")
    results["series"] = {"pass": ser.get("n", 0) >= 10 and "matched" in ser,
                          "n_editions": ser.get("n", 0), "matched": ser.get("matched", 0)}

    # copies
    if target_rel:
        cp = CSH.do_copies(ctx, target_rel)
        results["copies"] = {"pass": len(cp["members"]) >= 1, "n_members": len(cp["members"])}
    else:
        results["copies"] = {"pass": False}

    # exact nonexistent
    ex = CSH.do_exact(ctx, "zqxv-nonexistent-string-9931")
    results["exact_no_match"] = {"pass": ex["total"] == 0, "total": ex["total"]}

    # open + notes roundtrip
    if target_rel and results["inside_contents"].get("n_hits", 0) >= 1:
        ins = CSH.do_inside(ctx, target_rel, "contents")
        page_index = ins["hits"][0]["page_index"]
        op = CSH.do_open(ctx, target_rel, page_index, slug="selftest")
        nt = CSH.do_notes(ctx, "selftest")
        results["open_notes"] = {
            "pass": op["found"] and len(nt["opened"]) >= 1,
            "opened_found": op["found"], "n_opened_in_notes": len(nt["opened"]),
        }
    else:
        results["open_notes"] = {"pass": False}

    # note requires open (2026-09-15, F59 provenance enforcement). A note whose
    # cited page was never opened under the same slug must be refused and must
    # record nothing; one opened properly must still be accepted.
    if target_rel:
        slug = "selftest_prov"
        nd = CSH._notes_dir(None)
        for f in (nd / (slug + "_notes.jsonl"), nd / (slug + "_opened.jsonl")):
            if f.exists():
                f.unlink()
        tail = ' | %s | p0' % target_rel
        r_no_tail = CSH.do_note(ctx, slug, "no citation here")
        r_before = CSH.do_note(ctx, slug, "line" + tail)
        recorded_before = (nd / (slug + "_notes.jsonl")).exists()
        CSH.do_open(ctx, target_rel, 0, slug=slug)
        r_after = CSH.do_note(ctx, slug, "line" + tail)
        results["note_requires_open"] = {
            "pass": (r_no_tail.get("error") == "NOTE_REFUSED_NO_CITATION"
                     and r_before.get("error") == "NOTE_REFUSED_PAGE_NOT_OPENED"
                     and not recorded_before
                     and r_after.get("ok") is True),
            "no_tail": r_no_tail.get("error"), "before_open": r_before.get("error"),
            "recorded_before_open": recorded_before, "after_open_ok": r_after.get("ok"),
        }
        for f in (nd / (slug + "_notes.jsonl"), nd / (slug + "_opened.jsonl")):
            if f.exists():
                f.unlink()
    else:
        results["note_requires_open"] = {"pass": False, "reason": "no target"}

    # the inside/series CLI default is B2c (dense_first) while the Python
    # default stays None, so recorded numbers reproduce with explicit flags.
    if target_rel:
        want = [h["page_index"] for h in
                CSH.do_inside(ctx, target_rel, "contents", k=8,
                              caption_channel="dense_first")["hits"]]
        r = subprocess.run([PY, SHELF_PY, "--db", DB, "--shelf", SHELF,
                            "inside", target_rel, "contents", "--k", "8"],
                           capture_output=True, text=True, errors="replace")
        got = []
        for line in (r.stdout or "").splitlines():
            s = line.strip()
            if s.startswith("p") and "|" in s:
                try:
                    got.append(int(s.split()[0][1:]))
                except ValueError:
                    pass
        results["cli_caption_default_is_b2c"] = {
            "pass": bool(want) and got == want,
            "n_cli": len(got), "n_inprocess": len(want)}
    else:
        results["cli_caption_default_is_b2c"] = {"pass": False, "reason": "no target"}

    # citation guard at the answer boundary (2026-09-16, F61). Behaviour, not
    # installation: blocked once on a cited-but-unopened path, passes on a
    # second stop, passes when every citation was opened, passes with none.
    r = subprocess.run([PY, str(Path(__file__).resolve().parent / "c_stop_guard_selftest.py")],
                       capture_output=True, text=True, errors="replace")
    guard = {}
    try:
        guard = json.loads((L.STATE / "c_stop_guard_selftest.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    results["stop_guard_behaviour"] = {
        "pass": r.returncode == 0 and guard.get("all_pass") is True,
        "n_checks": guard.get("n_checks"), "n_passed": guard.get("n_passed")}

    # Phase 9.3.1: `find --compact N` prints exactly N one-line cards, and
    # `--show 1` prints the same family the compact list put first. This is the
    # Experiment H mechanism shipped in-session, so its shape is worth a test.
    r = subprocess.run([PY, SHELF_PY, "--db", DB, "--shelf", SHELF,
                        "find", "budget", "--compact", "10"],
                       capture_output=True, text=True, errors="replace")
    card_lines = [l for l in (r.stdout or "").splitlines() if l.startswith("#")]
    first_compact_family = ""
    if card_lines:
        parts = card_lines[0].split("|")
        first_compact_family = parts[0].split("  ", 1)[-1].strip() if parts else ""
    results["find_compact_prints_n_lines"] = {
        "pass": r.returncode == 0 and len(card_lines) == 10
                and "expand:" in (r.stdout or "") and "COVERAGE" in (r.stdout or ""),
        "n_card_lines": len(card_lines)}

    r2 = subprocess.run([PY, SHELF_PY, "--db", DB, "--shelf", SHELF,
                         "find", "budget", "--show", "1"],
                        capture_output=True, text=True, errors="replace")
    show_fam = ""
    for line in (r2.stdout or "").splitlines():
        if line.startswith("#1  family:"):
            show_fam = line.split("family:", 1)[1].strip()
            break
    results["find_show_matches_compact_rank"] = {
        "pass": (r2.returncode == 0 and bool(show_fam) and bool(first_compact_family)
                 and CSH._family_words(show_fam) == first_compact_family),
        "matched": bool(show_fam) and CSH._family_words(show_fam) == first_compact_family}

    # bogus subcommand -> exit 2 and usage, via the real CLI
    r = subprocess.run([PY, SHELF_PY, "--db", DB, "--shelf", SHELF, "bogus_command_xyz"],
                        capture_output=True, text=True)
    results["bogus_subcommand"] = {"pass": r.returncode == 2, "rc": r.returncode}

    all_pass = all(v.get("pass") for v in results.values())
    results["_overall"] = {"pass": all_pass}
    out = Path(a.out)
    out.write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(json.dumps(results, indent=1))

    # clean up the selftest notes file per the spec (2.3: "Delete the selftest notes file")
    nd = CSH._notes_dir(None)
    for p in nd.glob("selftest_*.jsonl"):
        p.unlink()

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
