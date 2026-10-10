#!/usr/bin/env python
"""c_verify_guard.py -- Stop hook: the session's notes, checked against the index.

Why a second hook instead of more rules in c_stop_guard.py: that guard compares the answer's
TEXT with what the session opened. It cannot know whether a quoted line is really on the page
it cites -- that needs the index, which is what `verify` does. This hook runs verify for the
notes this session wrote, and with the answer text from the transcript it also checks that
every figure in the answer appears in a note's quote (the one way left to be wrong while
every other check passes).

Same discipline as the citation guard, deliberately:
  * an answer that cited nothing is never blocked
  * it asks at most ONCE per turn (per session until Test 2, 2026-10-10, showed one block
    during `initialize` silencing it for a whole afternoon of questions), never on the
    re-entry it caused, so a session that disagrees always terminates
  * it prints the hook contract's JSON decision on stdout and exits 0

Installed by c_stack.py into the rung's .claude/settings.json as a second Stop hook.

  python engine/bin/c_verify_guard.py --selftest     # the decision logic, no corpus
"""
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import labpaths as L        # noqa: E402
import c_shelf as CS        # noqa: E402
import c_stop_guard as SG   # noqa: E402  (its transcript reader: one parser, not two)

MARKER_DIR = Path(os.environ.get("VERIFY_GUARD_MARKERS",
                                 str(L.SCRATCH / "verify_guard_markers")))
LOG = Path(os.environ.get("VERIFY_GUARD_LOG",
                          str(L.hook_log_dir() / "verify_guard__unlabelled.log")))
# How far back a note counts as this session's. A session is minutes; the window only has to
# be longer than one and shorter than the gap between them.
WINDOW_MIN = int(os.environ.get("VERIFY_GUARD_MINUTES", "180"))

# The reader never sees this exchange: after fixing it, give the whole answer again from
# the top as your first reply would read, without mentioning the check or what changed.
RESTATE = (" Then give the whole answer again from the top, as your first reply would "
           "read. Do not mention this check, what it raised, or what you changed.")
REASON_NOTES = ("Your notes do not survive checking against the index: {detail}. Do not hand "
                "this answer over until that is fixed -- re-open the page and re-make the "
                "note, or drop the figure.")
REASON_FIGURES = ("These figures in your answer appear in no note's quote: {detail}. Each "
                  "one either needs a note quoting a line that carries it, or it does not go "
                  "in the answer. (Rounding or a restated unit is the usual innocent cause.)")


def recent_note_slugs(notes_dir, window_min=WINDOW_MIN, now=None):
    """The slugs whose notes were written inside the window, most recent first.

    A session is minutes old, so a file whose mtime is older than the window belongs to an
    earlier session and blocking this answer over it would be wrong.
    """
    now = time.time() if now is None else now
    out = []
    d = Path(notes_dir)
    if not d.exists():
        return out
    for f in d.glob("*_notes.jsonl"):
        try:
            age_min = (now - f.stat().st_mtime) / 60.0
        except OSError:
            continue
        if age_min <= window_min and f.stat().st_size > 0:
            out.append((f.stat().st_mtime, f.name[: -len("_notes.jsonl")]))
    out.sort(reverse=True)
    return [slug for _m, slug in out]


_SLUG_RE = re.compile(r"--slug[\s=]+[\"']?([A-Za-z0-9_.-]+)")


def session_slugs(transcript_path):
    """The slugs this session's own commands used, in first-use order; None if unreadable.

    The recent-files window alone is not enough: a second question asked within the window
    picked up the first question's notes, its answer was checked against figures it never
    claimed, and the model's reply to that false alarm leaked into the answer.
    """
    try:
        lines = Path(transcript_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, TypeError):
        return None
    out = []
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        content = (ev.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                cmd = str((b.get("input") or {}).get("command") or "")
                for s in _SLUG_RE.findall(cmd):
                    if s not in out:
                        out.append(s)
    return out


def merge_figures(results):
    """A figure is unsupported only when NO slug of the session carries it.

    do_verify checks the answer against one slug's notes at a time, so a session that used
    two slugs had every figure from the other slug reported as unsupported.
    """
    if not results:
        return results
    sets = [{c["figure"] for c in r.get("unsupported", [])} for r in results]
    common = set.intersection(*sets) if sets else set()
    for r in results:
        r["unsupported"] = [c for c in r.get("unsupported", []) if c["figure"] in common]
        r["n_bad_answer"] = len(r["unsupported"])
    return results


def decide(results, marker_exists, stop_hook_active):
    """-> reason (a string) or None. Pure, so the block-once rule is testable.

    results: [{"slug":..., "n_bad":int, "n_bad_answer":int, "unsupported":[figures]}]
    """
    if stop_hook_active or marker_exists:
        return None
    if not results:                       # nothing was cited: never block
        return None
    bad_notes, bad_figs = [], []
    for r in results:
        if r.get("n_bad"):
            names = [c.get("error") for c in r.get("checks", []) if not c.get("ok")]
            bad_notes.append("%s (%s)" % (r["slug"], ", ".join(sorted(set(names)))))
        if r.get("n_bad_answer"):
            figs = [c["figure"] for c in r.get("unsupported", [])]
            bad_figs.append("%s: %s" % (r["slug"], ", ".join(figs[:6])))
    if bad_notes:
        return REASON_NOTES.format(detail="; ".join(bad_notes[:4]))
    if bad_figs:
        return REASON_FIGURES.format(detail="; ".join(bad_figs[:4]))
    return None


def check(cwd=None, transcript=None):
    """Verify every recent slug for the corpus this session is working in."""
    old_cwd = os.getcwd()
    if transcript:
        transcript = str(Path(transcript).resolve())   # before the chdir below
    try:
        if cwd:
            os.chdir(cwd)                     # _resolve reads the registry by cwd
        root, db, shelf = CS._resolve(None, None)
        if not (db and shelf):
            return [], "NO_REGISTERED_ROOT"
        ctx = CS.get_ctx(str(db), str(shelf), root)
        notes_dir = CS._notes_dir(root)
        slugs = recent_note_slugs(notes_dir)
        own = session_slugs(transcript) if transcript else None
        if own is not None:                   # the session's record is readable: use it
            slugs = [s for s in slugs if s in own]
        if not slugs:
            return [], None
        answer_text = ""
        if transcript:
            _opened, answer_text = SG.read_transcript(transcript)
        answer_file = None
        # The figure check is for document answers, which end in a Sources block. A status or
        # switch reply (Test 3, S01) states counts that are in no note; blocking it only taught
        # the model to spell its numbers out in words.
        if answer_text.strip() and SG.has_sources_block(answer_text):
            fd, answer_file = tempfile.mkstemp(suffix=".md", prefix="verify_guard_")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(answer_text)
        try:
            out = []
            for slug in slugs[:20]:
                res = CS.do_verify(ctx, slug, answer_path=answer_file)
                out.append({"slug": slug, "n_bad": res.get("n_bad", 0),
                            "n_bad_answer": res.get("n_bad_answer", 0),
                            "unsupported": (res.get("answer") or {}).get("unsupported", []),
                            "checks": res.get("checks", [])})
            return merge_figures(out), None
        finally:
            if answer_file:
                Path(answer_file).unlink(missing_ok=True)
    finally:
        os.chdir(old_cwd)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print(json.dumps({}))
        return 0

    session_id = str(payload.get("session_id") or "nosession")
    stop_hook_active = bool(payload.get("stop_hook_active"))
    MARKER_DIR.mkdir(parents=True, exist_ok=True)
    marker = MARKER_DIR / ("%s.blocked" % re.sub(r"[^A-Za-z0-9_.-]", "_", session_id))

    results, note = [], None
    try:
        results, note = check(cwd=payload.get("cwd"), transcript=payload.get("transcript_path"))
    except Exception as e:                     # a guard must never break the session
        note = "GUARD_ERROR:%s" % type(e).__name__

    key = SG.turn_key(payload.get("transcript_path"))
    reason = decide(results, SG.blocked_this_turn(marker, key), stop_hook_active)
    decision = {}
    if reason:
        marker.write_text("%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), key),
                          encoding="utf-8")
        decision = {"decision": "block", "reason": reason + RESTATE}
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "session": session_id[:40],
                "stop_hook_active": stop_hook_active,
                "slugs": [r["slug"] for r in results],
                "n_bad_notes": sum(r.get("n_bad", 0) for r in results),
                "n_unsupported_figures": sum(r.get("n_bad_answer", 0) for r in results),
                "note": note,
                "blocked": bool(reason),
            }) + "\n")
    except Exception:
        pass

    print(json.dumps(decision))
    return 0


def _selftest():
    checks = []
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        now = time.time()
        (d / "fresh_notes.jsonl").write_text("{}\n", encoding="utf-8")
        (d / "old_notes.jsonl").write_text("{}\n", encoding="utf-8")
        (d / "empty_notes.jsonl").write_text("", encoding="utf-8")
        os.utime(d / "old_notes.jsonl", (now - 600 * 60, now - 600 * 60))
        slugs = recent_note_slugs(d, window_min=180, now=now)
        checks.append(("picks_a_fresh_note", "fresh" in slugs))
        checks.append(("ignores_an_old_note", "old" not in slugs))
        checks.append(("ignores_an_empty_note", "empty" not in slugs))
        checks.append(("no_notes_dir_is_empty", recent_note_slugs(d / "nope", now=now) == []))

    with tempfile.TemporaryDirectory() as td:
        t = Path(td) / "t.jsonl"
        cmds = ['py c_shelf.py open "a.pdf" 3 --slug mine', 'py c_shelf.py note --slug mine2 "x"',
                'py c_shelf.py notes --slug mine']
        t.write_text("\n".join(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": c}}]}}) for c in cmds),
            encoding="utf-8")
        checks.append(("session_slugs_from_transcript", session_slugs(t) == ["mine", "mine2"]))
        checks.append(("unreadable_transcript_is_none", session_slugs(Path(td) / "nope") is None))
    two = [{"slug": "a", "n_bad": 0, "unsupported": [{"figure": "1"}, {"figure": "9"}]},
           {"slug": "b", "n_bad": 0, "unsupported": [{"figure": "2"}, {"figure": "9"}]}]
    merged = merge_figures(two)
    checks.append(("figure_in_any_slug_is_supported",
                   [c["figure"] for r in merged for c in r["unsupported"]] == ["9", "9"]))

    checks.append(("no_notes_never_blocks", decide([], False, False) is None))
    checks.append(("re_entry_never_blocks",
                   decide([{"slug": "s", "n_bad": 1}], False, True) is None))
    checks.append(("blocked_once_then_passes",
                   decide([{"slug": "s", "n_bad": 1}], True, False) is None))
    ok = [{"slug": "s", "n_bad": 0, "n_bad_answer": 0}]
    checks.append(("clean_notes_do_not_block", decide(ok, False, False) is None))
    bad = [{"slug": "s", "n_bad": 1, "n_bad_answer": 0,
            "checks": [{"ok": False, "error": "QUOTE_NOT_ON_PAGE"}]}]
    r = decide(bad, False, False)
    checks.append(("failed_note_blocks", bool(r) and "QUOTE_NOT_ON_PAGE" in r))
    fig = [{"slug": "s", "n_bad": 0, "n_bad_answer": 1,
            "unsupported": [{"figure": "987,654"}]}]
    r = decide(fig, False, False)
    checks.append(("unsupported_figure_blocks", bool(r) and "987,654" in r))

    n = sum(1 for _, o in checks if o)
    for name, o in checks:
        print("%-34s %s" % (name, "pass" if o else "FAIL"))
    print("verify guard: %d/%d pass" % (n, len(checks)))
    return 0 if n == len(checks) else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    sys.exit(main())
