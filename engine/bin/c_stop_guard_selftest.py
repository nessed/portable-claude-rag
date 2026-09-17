#!/usr/bin/env python
"""c_stop_guard_selftest.py -- 2026-09-16, Phase 2.2.

Tests the citation guard's BEHAVIOUR, not its installation. Four cases, each
driving the real hook as a subprocess with a synthetic transcript on stdin,
exactly as Claude Code would:

  1. an answer citing a path that was never opened  -> blocked once, with reason
  2. the same session stopping a second time        -> passes unconditionally
  3. an answer citing only paths that were opened   -> passes
  4. an answer citing nothing at all                -> passes

  python -u corpus-lab/bin/c_stop_guard_selftest.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import labpaths as L  # noqa: E402

GUARD = str(L.BIN / "c_stop_guard.py")


def transcript(tmp, opens, final_text):
    """A stream-json transcript: some `open` tool calls, then a final answer."""
    lines = []
    for rel, page in opens:
        lines.append(json.dumps({"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "tool_use", "name": "Bash", "input": {
                "command": 'py c_shelf.py open "%s" %d --slug s1' % (rel, page)}}]}}))
    lines.append(json.dumps({"type": "assistant", "message": {"role": "assistant",
        "content": [{"type": "text", "text": final_text}]}}))
    p = Path(tmp) / ("t%d.jsonl" % int(time.time() * 1000000))
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def run_guard(tpath, session_id, stop_hook_active=False, markers=None, v2=False,
              v2_via="arg"):
    """v2_via="arg" drives the guard the way c_stack.py installs it (--v2);
    "env" drives it through STOP_GUARD_V2=1. Both must work, and the argument
    form is the one that ships, because a hook command's shell is the host's
    choice and an env prefix can be silently dropped."""
    env = dict(os.environ)
    if markers:
        env["STOP_GUARD_MARKERS"] = str(markers)
    env.pop("STOP_GUARD_V2", None)
    if v2 and v2_via == "env":
        env["STOP_GUARD_V2"] = "1"
    payload = json.dumps({"session_id": session_id, "transcript_path": str(tpath),
                          "stop_hook_active": stop_hook_active})
    argv = [sys.executable, GUARD] + (["--v2"] if (v2 and v2_via == "arg") else [])
    p = subprocess.run(argv, input=payload, capture_output=True,
                       text=True, errors="replace", env=env)
    try:
        return json.loads((p.stdout or "").strip() or "{}"), p.returncode
    except Exception:
        return {"_unparsable": (p.stdout or "")[:200]}, p.returncode


def main():
    tmp = tempfile.mkdtemp(prefix="stopguard_")
    markers = Path(tmp) / "markers"
    checks = []

    def chk(name, cond, **extra):
        checks.append(dict(check=name, **{"pass": bool(cond)}, **extra))

    OPENED = "some_dir/a_report.pdf"
    OTHER = "other_dir/never_opened.pdf"

    # 1. cites a path it never opened -> blocked once
    t1 = transcript(tmp, [(OPENED, 7)],
                    "The figure is 4321, from %s page 7 and also %s." % (OPENED, OTHER))
    d1, rc1 = run_guard(t1, "sess-A", markers=markers)
    chk("cited_unopened_blocked", d1.get("decision") == "block", rc=rc1,
        decision=d1.get("decision"))
    chk("block_reason_names_the_path",
        OTHER.split("/")[-1] in (d1.get("reason") or ""), reason=(d1.get("reason") or "")[:90])
    chk("block_reason_offers_both_remedies",
        "open that page" in (d1.get("reason") or "")
        and "remove the citation" in (d1.get("reason") or ""))

    # 2. same session stops again -> passes unconditionally (both routes)
    d2a, _ = run_guard(t1, "sess-A", markers=markers)
    chk("second_stop_passes_via_marker", d2a.get("decision") != "block",
        decision=d2a.get("decision"))
    d2b, _ = run_guard(t1, "sess-B", stop_hook_active=True, markers=markers)
    chk("stop_hook_active_passes", d2b.get("decision") != "block",
        decision=d2b.get("decision"))

    # 3. cites only what it opened -> passes
    t3 = transcript(tmp, [(OPENED, 7)], "The figure is 4321, from %s page 7." % OPENED)
    d3, _ = run_guard(t3, "sess-C", markers=markers)
    chk("cited_opened_passes", d3.get("decision") != "block", decision=d3.get("decision"))

    # 4. cites nothing -> passes
    t4 = transcript(tmp, [(OPENED, 7)],
                    "I could not find supporting evidence for this in the folder.")
    d4, _ = run_guard(t4, "sess-D", markers=markers)
    chk("no_citation_passes", d4.get("decision") != "block", decision=d4.get("decision"))

    # 5. the guard never rewrites an answer
    chk("guard_returns_only_a_decision",
        set(d1.keys()) <= {"decision", "reason"} and set(d3.keys()) <= set())

    # 6. exit code is always 0 -- a hook that errors must not break a session
    chk("exit_code_zero", rc1 == 0, rc=rc1)

    # ---------------------------------------------------------------- #
    # Guard v2 (Phase 9.3.3), STOP_GUARD_V2=1. v1's blind spot was a citation
    # written as prose -- Session Log A.2.1's "Title, p.239" names no path, so
    # the path matcher never saw it -- and F62's other half, an answer of bare
    # figures that cites nothing and so passes a guard that only inspects
    # citations. Each case below is one of those, plus the ones that must NOT
    # fire: a year is not a page number, and an honest refusal is not a figure.
    # ---------------------------------------------------------------- #
    # 7. prose title with a page that WAS opened -> passes
    t7 = transcript(tmp, [(OPENED, 239)],
                    "Total development expenditure was 4,321 billion "
                    "(Annual Report, p.239).")
    d7, _ = run_guard(t7, "v2-A", markers=markers, v2=True)
    chk("v2_prose_page_opened_passes", d7.get("decision") != "block",
        decision=d7.get("decision"))

    # 8. prose title with a page that was NEVER opened -> blocks
    t8 = transcript(tmp, [(OPENED, 7)],
                    "Total development expenditure was 4,321 billion "
                    "(Annual Report, p.239).")
    d8, _ = run_guard(t8, "v2-B", markers=markers, v2=True)
    chk("v2_prose_page_never_opened_blocks", d8.get("decision") == "block",
        decision=d8.get("decision"))
    chk("v2_block_reason_names_the_page", "239" in (d8.get("reason") or ""),
        reason=(d8.get("reason") or "")[:90])

    # 9. figures with no citation whatsoever -> blocks
    t9 = transcript(tmp, [(OPENED, 7)],
                    "Development spending rose from 1,200 billion to 4,321 billion "
                    "over the decade, about 4.1 percent of GDP.")
    d9, _ = run_guard(t9, "v2-C", markers=markers, v2=True)
    chk("v2_figures_no_citation_blocks", d9.get("decision") == "block",
        decision=d9.get("decision"))

    # 10. an honest "no supporting page was opened" answer with no figures -> passes
    t10 = transcript(tmp, [(OPENED, 7)],
                     "This folder does not hold that figure. Sources: none -- no "
                     "supporting page was opened.")
    d10, _ = run_guard(t10, "v2-D", markers=markers, v2=True)
    chk("v2_honest_no_answer_passes", d10.get("decision") != "block",
        decision=d10.get("decision"))

    # 11. a Sources block whose every line was opened -> passes
    t11 = transcript(tmp, [(OPENED, 12)],
                     "Development expenditure was 4,321 billion.\n\n"
                     "Sources\n4,321 billion | %s | p12 | \"Development Expenditure 4,321\"\n"
                     % OPENED)
    d11, _ = run_guard(t11, "v2-E", markers=markers, v2=True)
    chk("v2_sources_block_all_opened_passes", d11.get("decision") != "block",
        decision=d11.get("decision"))

    # 12. a Sources block naming a page of that file that was never opened -> blocks
    t12 = transcript(tmp, [(OPENED, 12)],
                     "Development expenditure was 4,321 billion.\n\n"
                     "Sources\n4,321 billion | %s | p88 | \"Development Expenditure 4,321\"\n"
                     % OPENED)
    d12, _ = run_guard(t12, "v2-F", markers=markers, v2=True)
    chk("v2_sources_block_wrong_page_blocks", d12.get("decision") == "block",
        decision=d12.get("decision"))

    # 13. a fiscal year and a bare calendar year are NOT page references
    t13 = transcript(tmp, [(OPENED, 7)],
                     "The 2012-13 edition and the 2019 edition report it differently; "
                     "see %s page 7." % OPENED)
    d13, _ = run_guard(t13, "v2-G", markers=markers, v2=True)
    chk("v2_years_are_not_page_refs", d13.get("decision") != "block",
        decision=d13.get("decision"))

    # 14. `p.239` written with the period is parsed at all (the A.2.1 shape)
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("c_stop_guard_mod", GUARD)
    _g = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_g)
    chk("v2_page_ref_regex_parses_period_form", _g.page_refs("see p.239 there") == [239],
        parsed=_g.page_refs("see p.239 there"))
    chk("v2_page_ref_regex_parses_page_word", _g.page_refs("see page 42 there") == [42])
    chk("v2_page_ref_regex_ignores_years",
        _g.page_refs("the 2012-13 and 2019 editions") == [],
        parsed=_g.page_refs("the 2012-13 and 2019 editions"))

    # 15. v2 blocks at most once, and a second stop still passes
    d15, _ = run_guard(t8, "v2-B", markers=markers, v2=True)
    chk("v2_second_stop_passes", d15.get("decision") != "block",
        decision=d15.get("decision"))

    # 16. the env form works too, so either switch turns v2 on
    d16e, _ = run_guard(t9, "v2-H", markers=markers, v2=True, v2_via="env")
    chk("v2_env_switch_also_works", d16e.get("decision") == "block",
        decision=d16e.get("decision"))

    # 17. the installed hook command really carries the switch. This is the check
    # that would have caught an env prefix the host's shell quietly dropped.
    try:
        import c_stack as CST
        hook_cmd = '"{}" "{}" --v2'.format(sys.executable, L.BIN / "c_stop_guard.py")
        chk("installed_hook_command_carries_v2",
            "--v2" in hook_cmd and "c_stop_guard.py" in hook_cmd, cmd=hook_cmd[-40:])
        del CST
    except Exception as e:
        chk("installed_hook_command_carries_v2", False, err=str(e)[:80])

    # 18. with the flag OFF, every v2-only case passes -- so P5-P8 reproduce
    d16a, _ = run_guard(t8, "v1-B", markers=markers, v2=False)
    d16b, _ = run_guard(t9, "v1-C", markers=markers, v2=False)
    chk("v1_unchanged_without_the_flag",
        d16a.get("decision") != "block" and d16b.get("decision") != "block",
        prose=d16a.get("decision"), figures=d16b.get("decision"))

    ok = all(c["pass"] for c in checks)
    rec = {"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "n_checks": len(checks), "n_passed": sum(1 for c in checks if c["pass"]),
           "all_pass": ok, "checks": checks}
    (L.STATE / "c_stop_guard_selftest.json").write_text(json.dumps(rec, indent=1),
                                                        encoding="utf-8")
    print(json.dumps({k: v for k, v in rec.items() if k != "checks"}, indent=1))
    for c in checks:
        if not c["pass"]:
            print("FAILED: %s  %s" % (c["check"],
                                      {k: v for k, v in c.items()
                                       if k not in ("check", "pass")}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
