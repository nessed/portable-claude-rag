#!/usr/bin/env python
"""c_stop_guard.py -- Stop hook: a citation guard at the answer boundary.

2026-09-16. F59 measured that enforcing provenance at the `note` boundary does
nothing, because the answer never passes through the note: the session notes
the pages it opened and then names extra paths in its final message, where no
check existed. `cited_unopened_total` went 8 -> 10 with the note guard live.

So the check moves to the only place the answer actually is. On Stop this hook
reads the session transcript, collects every `open "<path>" <page>` the session
issued, collects every path-like string in the final assistant message, and if
a cited path was never opened it blocks ONCE with a reason.

**This is not the S2 hook.** S2 denied the search tools outright and forced a
route; it was a controller. This reads one property of a finished answer and
asks once. It never edits text, never blocks an answer that cites nothing, and
a second stop passes unconditionally -- so a session that disagrees, or that
cannot open the page, always terminates.

Installed by c_stack.py into the rung's .claude/settings.json.
Exit 0 with a JSON decision, as Claude Code's Stop hook contract requires.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import labpaths as L  # noqa: E402

LOG = Path(os.environ.get("STOP_GUARD_LOG",
                          str(L.hook_log_dir() / "stop_guard__unlabelled.log")))
MARKER_DIR = Path(os.environ.get("STOP_GUARD_MARKERS",
                                 str(L.SCRATCH / "stop_guard_markers")))

EXTS = r"(?:pdf|xlsx|xls|docx|doc|csv|txt|md|pptx|ppt)"
# a path-like string in free answer text: at least one separator, then a known
# corpus extension. Directory names in this corpus contain spaces.
CITED_RE = re.compile(
    r"[A-Za-z0-9_\-.()\[\]][A-Za-z0-9_\-./\\()\[\] ]{0,180}?[/\\][A-Za-z0-9_\-.()\[\] ]{1,120}?\." + EXTS,
    re.I)
# `... c_shelf.py open "<rel>" <page>` in a Bash command string
OPEN_RE = re.compile(r"open\s+(?P<q>[\"'])(?P<rel>.+?)(?P=q)\s+(?P<page>\d+)", re.S)
OPEN_BARE_RE = re.compile(r"open\s+(?P<rel>\S+\." + EXTS + r")\s+(?P<page>\d+)", re.I)

# The reader never sees this exchange: after fixing it, give the whole answer again from
# the top as your first reply would read, without mentioning the check or what changed.
RESTATE = (" Then give the whole answer again from the top, as your first reply would "
           "read. Do not mention this check, what it raised, or what you changed.")
REASON = ("You cited {path} without opening it. Either open that page and quote the "
          "supporting line, or remove the citation. Do not add any figure you have "
          "not opened.")

# --------------------------------------------------------------------- #
# Guard v2 (Phase 9.3.3), behind STOP_GUARD_V2=1 so every recorded P5-P8 number
# still reproduces from the v1 path. c_stack.py setup sets the flag in the hook
# command it installs.
#
# Session Log A.2.1: the v1 guard passes a citation written as prose -- "Title,
# p.239" -- because it only matches path-like strings, and F62 recorded the
# other half of the same hole, an answer full of figures that cites nothing at
# all and so satisfies a guard that only inspects citations it can see. v2 adds
# three checks at the same boundary, with the same manners: it asks once, never
# edits text, and always lets a second stop through.
# --------------------------------------------------------------------- #
# Either the env var or a `--v2` argument turns it on. The argument is what
# c_stack.py installs: a hook command is run by whichever shell the host picks,
# and a `set VAR=1 &&` prefix means different things to cmd.exe and to bash, so
# a shell-independent switch is the only one that cannot fail silently.
V2_ON = (os.environ.get("STOP_GUARD_V2") == "1") or ("--v2" in sys.argv)

# Test 2 (2026-10-10). Two more checks, each behind its own argument so the lab's
# recorded numbers still reproduce without them; hooks.cmd passes both.
#   --require-header "<line>"  the reply must begin with this line (the ON marker)
#   --ban-phrases              the reply must not use the tool's own vocabulary
REQUIRE_HEADER = None
for _i, _a in enumerate(sys.argv):
    if _a == "--require-header" and _i + 1 < len(sys.argv):
        REQUIRE_HEADER = sys.argv[_i + 1]
BAN_PHRASES_ON = "--ban-phrases" in sys.argv
#   --sources-last             nothing may follow the Sources block (Test 3: Q03, S05 put a
#                              caution after it, where a reader checking sources stops reading)
SOURCES_LAST_ON = "--sources-last" in sys.argv
SOURCE_LINE_RE = re.compile(r"^\s*(?:[-*]|\d+\.|\|)|\s\|\s")
REASON_SOURCES_LAST = ("End the reply with the Sources block. Move the text that now follows it "
                       "above the block.")
# Vocabulary that belongs to the machinery, not to an answer. Deliberately NOT the bare word
# "index": economics prose is full of indices, and the status reply has to say when the
# index was built. The phrases here are the ones Test 2 actually saw leak (Q17, Q10, S02);
# "rewrite" alone is ordinary English ("rewrite its table", Q16) and matches only as the search term.
MACHINERY_RE = re.compile(
    r"(?<![A-Za-z])(?:the shelf|shelf (?:returned|gave|listed|showed)|"
    r"(?:\d+|two|three|four|five|six|several|my|the) rewrites|"
    r"rewrites? (?:of|for) (?:the|my) (?:question|query|search)|(?:query|search) rewrites?|"
    r"the whole index|occur in the index|search(?:ed|ing)? the index|not indexed|"
    r"hooks?(?![A-Za-z])|slugs?(?![A-Za-z])|exit code|harness|coverage line|"
    r"(?:stop|verify|note|citation) guard|c_shelf|99_scratch|engine[\\/]bin)", re.I)
REASON_HEADER = ("Begin the reply with the line `{h}` on its own, before anything else; "
                 "every reply while the research assistant is on starts with it.")
REASON_MACHINERY = ("The reply uses the tool's own vocabulary ({words}). Say it in plain "
                    "words a reader would use -- what you looked for and what you found -- "
                    "with no account of how the search was run.")

# A page reference in prose. The leading literal `p` is what keeps a fiscal year
# (2012-13) or a bare year (2019) from being read as a page number.
PAGE_REF_RE = re.compile(r"(?<![A-Za-z0-9])p\.?\s?(\d{1,4})(?![0-9-])", re.I)
PAGE_WORD_RE = re.compile(r"(?<![A-Za-z0-9])page\s+(\d{1,4})(?![0-9-])", re.I)
SOURCES_HEAD_RE = re.compile(r"^\s*#{0,6}\s*\**\s*sources\s*\**\s*:?\s*$", re.I | re.M)
# a Sources line: `... | <path> | p<n> | ...`
SOURCES_LINE_RE = re.compile(
    r"\|\s*(?P<path>[^|]*?\.(?:pdf|xlsx|xls|docx|doc|csv|txt|md|pptx|ppt))\s*\|\s*p\.?\s?(?P<page>\d{1,4})\b",
    re.I)
UNIT_RE = re.compile(
    r"(?<![A-Za-z])(billion|million|percent|%|rs|rupees|tonnes|thousand)(?![A-Za-z])", re.I)
NUMBER_RE = re.compile(r"(?<![A-Za-z0-9.,])\d[\d,]*(?:\.\d+)?(?![A-Za-z])")

REASON_PAGE = ("You cited page {n} but never opened a page with that index. Open it and "
               "quote the line, or remove the citation.")
REASON_NO_CITE = ("Your answer gives figures but cites no opened page. Add a Sources "
                  "block, or state that no supporting page was opened.")


def page_refs(text):
    """Every page number the answer claims, as ints."""
    out = []
    for rx in (PAGE_REF_RE, PAGE_WORD_RE):
        for m in rx.finditer(text or ""):
            try:
                out.append(int(m.group(1)))
            except ValueError:
                pass
    return out


def has_sources_block(text):
    return bool(SOURCES_HEAD_RE.search(text or "")) or bool(
        re.search(r"(?<![A-Za-z])sources\s*:", text or "", re.I))


def prose_after_sources(text):
    """True if a Sources block is followed by ordinary prose: a non-empty line, after at least
    one source line, that is neither a list item nor a table row."""
    t = text or ""
    heads = list(SOURCES_HEAD_RE.finditer(t))
    if not heads:
        return False
    tail = t[heads[-1].end():].splitlines()
    seen_source = False
    for line in tail:
        if not line.strip():
            continue
        if SOURCE_LINE_RE.search(line):
            seen_source = True
            continue
        if seen_source:
            return True
    return False


def figures_without_citation(text):
    """True if the answer states a figure -- a number with a unit word within
    three tokens -- and carries no page reference and no Sources block anywhere.
    That is F62's 'guard satisfied by silence'."""
    t = text or ""
    if page_refs(t) or has_sources_block(t):
        return False
    toks = re.findall(r"\S+", t)
    for i, tok in enumerate(toks):
        if not NUMBER_RE.search(tok):
            continue
        window = toks[max(0, i - 3):i + 4]
        if any(UNIT_RE.search(w) for w in window):
            return True
    return False


def segs_of(s):
    return [x for x in str(s or "").replace("\\", "/").lower().split("/") if x]


def tail2(s):
    return "/".join(segs_of(s)[-2:])


def same_path(cited, opened):
    """Directory names in this corpus contain spaces, so a path lifted out of
    free answer text keeps whatever prose word preceded it ("from some_dir/x.pdf").
    Comparing those literally marks a file the session DID open as unopened,
    which turns the guard into a false-positive generator -- it blocked the
    opened file in its own self-test. The filename must match exactly and the
    parent directory by suffix; that is the same rule scoring.py uses."""
    a, b = segs_of(cited), segs_of(opened)
    if not a or not b or a[-1] != b[-1]:
        return False
    if len(a) >= 2 and len(b) >= 2:
        return a[-2].endswith(b[-2]) or b[-2].endswith(a[-2])
    return True


def read_transcript(path, with_pages=False):
    """Returns (opened_paths, final_assistant_text), or with_pages=True
    (opened_paths, final_text, {(path, page_index)}) -- guard v2 needs the page
    a path was opened at, not only that it was opened."""
    opened, texts = set(), []
    pairs = set()
    p = Path(path) if path else None
    if not p or not p.exists():
        return (opened, "", pairs) if with_pages else (opened, "")
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        msg = d.get("message") or {}
        if d.get("type") == "assistant" or msg.get("role") == "assistant":
            content = msg.get("content")
            if isinstance(content, list):
                chunk = []
                for c in content:
                    if c.get("type") == "tool_use":
                        inp = c.get("input") or {}
                        cmd = inp.get("command") or ""
                        for rx in (OPEN_RE, OPEN_BARE_RE):
                            for m in rx.finditer(cmd):
                                opened.add(m.group("rel"))
                                try:
                                    pairs.add((m.group("rel"), int(m.group("page"))))
                                except (ValueError, IndexError):
                                    pass
                    elif c.get("type") == "text":
                        chunk.append(c.get("text") or "")
                if chunk:
                    texts.append("\n".join(chunk))
            elif isinstance(content, str):
                texts.append(content)
    final = texts[-1] if texts else ""
    return (opened, final, pairs) if with_pages else (opened, final)


def turn_key(path):
    """The uuid of the last real user message in the transcript: one per turn. The guards
    ask at most once per TURN -- a session with the professor runs for hours and dozens of
    questions, and a once-per-session marker (Test 2) meant the first block during
    `initialize` silenced the guard for everything that followed. Re-entry within a turn is
    still covered by stop_hook_active, so a session that disagrees always terminates."""
    p = Path(path) if path else None
    if not p or not p.exists():
        return "noturn"
    key = None
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or '"user"' not in line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") != "user":
            continue
        content = (d.get("message") or {}).get("content")
        if isinstance(content, list):
            if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
                continue
            text = " ".join((c.get("text") or "") for c in content
                            if isinstance(c, dict) and c.get("type") == "text")
        else:
            text = str(content or "")
        if text.lstrip().startswith("Stop hook feedback"):
            continue
        key = d.get("uuid") or key
    return key or "noturn"


def blocked_this_turn(marker, key):
    if not marker.exists():
        return False
    try:
        return marker.read_text(encoding="utf-8", errors="replace").strip().endswith(" " + key)
    except OSError:
        return False


def cited_paths(text):
    out = []
    for m in CITED_RE.finditer(text or ""):
        tok = m.group(0).strip().strip("`'\"()[],.")
        if len(tok) > 6:
            out.append(tok)
    return out


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print(json.dumps({}))
        return 0

    session_id = str(payload.get("session_id") or "nosession")
    transcript = payload.get("transcript_path")
    stop_hook_active = bool(payload.get("stop_hook_active"))

    MARKER_DIR.mkdir(parents=True, exist_ok=True)
    marker = MARKER_DIR / ("%s.blocked" % re.sub(r"[^A-Za-z0-9_.-]", "_", session_id))

    decision = {}
    reason = None
    unopened = []

    v2_hit = None
    rules = []
    key = turn_key(transcript)
    # Ask at most once per turn, and never on a re-entry the hook caused.
    if not stop_hook_active and not blocked_this_turn(marker, key):
        opened, final, pairs = read_transcript(transcript, with_pages=True)
        for c in cited_paths(final):
            if not any(same_path(c, o) for o in opened):
                unopened.append(c)
        if unopened:
            reason = REASON.format(path=tail2(unopened[0]))
        elif V2_ON:
            opened_pages = {pg for _rel, pg in pairs}
            # (1) every Sources line must name a page that was opened from that
            # file, under the same filename-exact/parent-by-suffix rule as v1.
            for m in SOURCES_LINE_RE.finditer(final):
                path, pg = m.group("path").strip(), int(m.group("page"))
                if not any(same_path(path, rel) and pg == opened_pg
                           for rel, opened_pg in pairs):
                    v2_hit = "sources_line_not_opened"
                    reason = REASON_PAGE.format(n=pg)
                    break
            # (2) a page number in prose that no opened page carries. This is the
            # A.2.1 hole: "Title, p.239" names no path, so v1 never saw it.
            if reason is None:
                for n in page_refs(final):
                    if n not in opened_pages:
                        v2_hit = "page_ref_not_opened"
                        reason = REASON_PAGE.format(n=n)
                        break
            # (3) figures and no citation at all -- F62's guard satisfied by silence.
            if reason is None and figures_without_citation(final):
                v2_hit = "figures_no_citation"
                reason = REASON_NO_CITE
        if v2_hit:
            rules.append(v2_hit)
        # (4) and (5): the ON header and the machinery vocabulary. Both are reported in the
        # same block as the citation problem, so one round trip fixes everything.
        extra = []
        if REQUIRE_HEADER and final.strip() and not final.lstrip().startswith(REQUIRE_HEADER):
            extra.append(REASON_HEADER.format(h=REQUIRE_HEADER))
            rules.append("missing_header")
        if BAN_PHRASES_ON and final.strip():
            words = sorted({m.group(0).lower() for m in MACHINERY_RE.finditer(final)})
            if words:
                extra.append(REASON_MACHINERY.format(words=", ".join(words[:6])))
                rules.append("machinery_words")
        if SOURCES_LAST_ON and prose_after_sources(final):
            extra.append(REASON_SOURCES_LAST)
            rules.append("prose_after_sources")
        if extra:
            reason = " ".join(([reason] if reason else []) + extra)
            v2_hit = v2_hit or rules[0]
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
                "n_cited_unopened": len(unopened),
                "blocked": bool(reason),
                "v2": V2_ON,
                "v2_rule": v2_hit,
                "rules": rules,
                "turn": key[:8],
            }) + "\n")
    except Exception:
        pass

    print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
