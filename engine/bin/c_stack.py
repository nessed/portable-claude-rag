#!/usr/bin/env python
"""c_stack.py -- Stage 4 of the approach-C contract, built 2026-09-15.

Installs and removes the s7_shelf stack in a corpus rung: exactly two files,
RUNG/.claude/settings.json and RUNG/CLAUDE.md, both owned by this script and
both listed in a state file written BEFORE either is created, so a teardown
after a crash still knows what to remove and can refuse to remove anything
else.

  python corpus-lab/bin/c_stack.py setup    --corpus RUNG
  python corpus-lab/bin/c_stack.py status   --corpus RUNG
  python corpus-lab/bin/c_stack.py teardown --corpus RUNG
  python corpus-lab/bin/c_stack.py selftest          # round-trip, no rung touched

Exit codes: 0 ok, 2 usage, 3 refused (ANOTHER_STACK_PRESENT / FOREIGN_FILES_PRESENT).

The CLAUDE.md text is the Stage 4.2 text from plans_fable/C_SHELF_FIRST_BUILD.md
verbatim, with {PY} and {SHELF} substituted and exactly three changes, all
recorded in the 2026-09-15 master prompt: the frozen retrieval flags on every
example, the rewrite instruction on `find`, and the open-before-cite sentence
in section 3. It contains no corpus, family, title, label or year string.

2026-10-10, after Test 2 of the paste-in package (results/test 2/SCORECARD.md): three
more changes. Section 3 turns the "check the prose before declaring a series absent"
sentence into two concrete commands (Q03 declared a series started where the tables
start; the earlier years were in a sentence). Section 4 says how a no-evidence answer
reads (Q17 described its search in the tool's vocabulary). Section 6 names the
vocabulary the final check now sends back.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import labpaths as L  # noqa: E402
import stack as STACK  # noqa: E402

# --------------------------------------------------------------------- #
# The frozen retrieval configuration (Phase 4 FREEZE). Every example in
# CLAUDE.md carries these, so what the live battery measures is exactly the
# configuration the offline gates measured.
# --------------------------------------------------------------------- #
FROZEN_FUSION = "rrf"
FROZEN_CAPTION_CHANNEL = "lex"   # document channel: E1, highest dev recall@10 (F49 FREEZE)
FROZEN_PAGE_CAPTION = "first"    # page channel: B2, per F48

CLAUDE_MD_TEMPLATE = """# How to answer questions from this research folder

Built-in Grep and Glob miss most of this tree (PDFs, spreadsheets, scanned files). Do
not rely on them. This folder has a shelf: one card per document saying what
publication it is, which year's edition, and which tables it lists. Work in two moves:
choose the documents from the shelf, then search inside them. Never search all pages at
once.

## 1. Find the publication on the shelf

    "{PY}" "{SHELF}" find "<the question as asked>" --fusion {FUSION} --caption-channel {CAPCHAN} --compact 40 --q "<rewrite>" --q "<rewrite>" --q "<rewrite>"

The list is forty publications, one per line, best-guess first. Read the whole list before
choosing. Pick up to three by asking *which kind of publication would print this table* --
a statistical yearbook, a budget document, a survey -- not by how many of your words appear
in the line. Then print their full cards with `--show i,j,k` and confirm the editions with
`have`.

Add three to five `--q "<rewrite>"` arguments of your own. They are searched alongside
the question and they matter more than anything else you do here. Write them as:

- the row name as a statistical publication would print it -- a noun phrase, no verbs;
- the publication together with the fiscal year, written like 2012-13;
- the two to four nouns that name the subject, the place and the period, with every
  question word and conversational filler dropped.

Read the families it returns: each is a publication with the editions (fiscal years)
held. Decide which publication and which year(s) answer the question. If the question
names a publication or a year, confirm it is held:

    "{PY}" "{SHELF}" have "<publication words>" --fy <year like 2012-13>

If it prints NO_EDITION_FOR or NO_FAMILY_MATCHES, the answer is that we do not hold
that document; report the editions that are held and stop. Do not substitute another
year's edition without saying so.

## 2. Find the page inside the document

    "{PY}" "{SHELF}" inside "<path exactly as printed>" "<the subject in a few words>"
    "{PY}" "{SHELF}" tables "<path>" --grep "<word>"
    "{PY}" "{SHELF}" table "<path>" --page <page_index>

tables lists the captions the index found. When the question is the value in a particular
CELL, `table` reads that page's grid from the PDF itself, cell by cell, which `open` cannot
do (the text layer runs a table's columns together). It needs the PDF on disk, and its cells
are NOT citable: a cell read from the layout may be absent from the index's text layer, and
the note guard checks quotes against that text. So use `table` to find the cell and its
neighbours, then open the page and quote the line you actually cite.

For a question about how something changed over years, walk the editions in one go:

    "{PY}" "{SHELF}" series "<the table row in a few words>" --family "<publication words>" --from 2015-16

series shows you the matching line per edition. It does not open pages; you still must
open the page in each edition you use.

The figure for a fiscal year is usually printed again, revised, in the next one or two
editions. When a question names a year, check the edition for that year and the one or
two after it; report which edition and vintage (provisional, revised, final) each figure
comes from, and if they disagree show both.

## 3. Open before you cite

    "{PY}" "{SHELF}" open "<path>" <page_index> --slug <slug>

A line printed by find, inside, series or tables is a search result, not evidence. You
may cite a file and page only after you have opened that page with open and quoted the
supporting line or cell in a note.

page_index is the PDF's own page number: a document's first page is p{FIRST}, so
`open "<path>" 20` opens PDF page 20 and a `p20` citation is the page a reader will find in
their viewer, with no arithmetic. Cite the page_index.

Before you report that a series starts later than asked, stops early, or is absent, do two
more things, because a table's first column is not the start of the series: the same
document, an older budget document or a working paper often states the earlier years in a
sentence. First, run exact on the row name as the table prints it (for example
"Own-Source Revenue") and open the matching pages in documents that are NOT the table you
already have -- budget documents, papers, older editions. Second, run find again with
rewrites that pair the row name with each of the two fiscal years before the table's first
column (for example "Own-Source Revenue 2011-12"). Only if both turn up nothing may you say
the folder holds nothing earlier -- and then write "the earliest I could find", never that
the series "genuinely" or "actually" starts there. Saying a figure is not in the folder
when it is there is as damaging as citing the wrong page, and no quote check will catch it,
because the quote you took was real.

Pick a short slug (letters and digits) for this question and reuse it in every open,
note and notes command. You may not cite, quote or take a number from a page you have
not opened. For a literal identifier (an SRO number, a demand number, a code) run
exact and, if TOTAL_PAGES_MATCHING=0, say that no readable page contains it:

    "{PY}" "{SHELF}" exact "<the literal string>"

## 4. Save what you find, then answer only from the notes

After opening a useful page:

    "{PY}" "{SHELF}" note --slug <slug> "<the exact line or table cell, verbatim> | <path> | p<page_index>"

note will refuse a line whose page you have not opened with open under the same slug; if it
refuses, open the page first. It also refuses a quote that does not occur on the page it
cites (NOTE_REFUSED_QUOTE_NOT_ON_PAGE), so copy the line from the page text you just
printed rather than writing it from memory.

Then print and answer from them:

    "{PY}" "{SHELF}" notes --slug <slug>

Every number in the answer carries its file path, page_index and the verbatim line. If
two editions or two copies disagree, show both with citations. If the notes are empty
after you have looked, say so the way a colleague would: name the subject words you
searched for and the publications you looked in, by their titles, and say that no readable
page mentions them. Do not describe how you searched -- no count of searches, no tool
vocabulary. Then say, once and in one sentence, how many files in the folder could not be
read (the COVERAGE line gives the counts). Never supply a figure from memory or from a
listing.

End every answer with a `Sources` block: one line per figure you used, in the form
`<figure> | <path> | p<page_index> | "<the verbatim line>"`. If you found nothing, the
`Sources` block says `none -- no page in this folder mentions <the subject words>` and the
answer says so.
A number without a Sources line does not go in the answer.

Before you hand the answer over, check the notes against the index:

    "{PY}" "{SHELF}" verify --slug <slug> --answer <path to your answer>

verify re-derives every note: the file is in the index, the page exists, and the quoted
text is on that page. Exit code 1 means at least one note failed. Do not hand over an
answer built on notes that failed -- fix the note or drop the figure. `notes` prints what
you wrote; `verify` is what checks it.

With --answer, it also reads the answer and checks every FIGURE in that prose against the
quotes in the notes, because a correct quote sitting next to a different number is the one
way an answer can be wrong while the notes all pass. Years, single digits and your own
citation tails are ignored; anything else it cannot find in a note is printed as
`NOT IN ANY NOTE QUOTE`, which is usually rounding or a restated unit ("610.5 million" for a
page that prints 610.456) and occasionally a figure you made up. Fix it by citing a line
that carries the figure you are stating, or by dropping it.

## 5. Copies

The folder holds partial copies, drafts and older versions of the same publications; the
shelf marks the complete one with * and hides the rest from find. Use the complete one
unless the question is about the copies, in which case run:

    "{PY}" "{SHELF}" copies "<path>"

Do not name copies you did not open.

## 6. How the answer reads

The person asking is a researcher, not an engineer, and should feel they are simply talking
to you. Lead with the finding, in plain English, the way you would answer in any
conversation. Keep the machinery out of the prose: no slugs, notes, COVERAGE lines, guards,
checks, exit codes or command names, and no words like "index", "harness" or "flagged",
and none of the tool's own: "shelf", "rewrites", "families", "hooks", "slug", "coverage
line". A final check reads every reply before it is shown and sends it back if it finds
that vocabulary; write it right the first time.

If a check sends your answer back, fix what it found and give the whole answer again from
the top, as though it were your first reply. Do not mention the check, what it raised, or
what you changed.
"""


# Paste-in layout (2026-10-10): appended to the frozen template when the procedure is
# written as engine/ANSWER.md. While the assistant is ON the model may write only under
# engine/, so the draft that `verify --answer` reads has to live there.
PASTE_IN_ADDENDUM = """
## 7. Where your working files go

While the research assistant is on you may not create or change any file in this folder
outside `engine/`; the hooks refuse it. Write the draft answer that `verify --answer` reads
to `engine/99_scratch/<slug>_answer.md`, and nothing else anywhere. Every command above is
run from this folder with the relative paths exactly as printed.

Every reply while the research assistant is on -- an answer, a one-line status, a refusal --
begins with the line `(research assistant on)`. The final check sends back a reply that
does not.
"""


# First line of the generated folder instructions. Used as a content-level
# ownership test so a moved install can still recognise its own file.
CLAUDE_MD_TITLE = CLAUDE_MD_TEMPLATE.splitlines()[0]


def claude_md_text(py_exe, shelf_cli):
    return (CLAUDE_MD_TEMPLATE
            .replace("{PY}", str(py_exe))
            .replace("{SHELF}", str(shelf_cli))
            .replace("{FUSION}", FROZEN_FUSION)
            .replace("{CAPCHAN}", FROZEN_CAPTION_CHANNEL)
            .replace("{PAGECAP}", FROZEN_PAGE_CAPTION)
            .replace("{FIRST}", str(L.FIRST_PAGE_INDEX)))


# --------------------------------------------------------------------- #
# deny list
# --------------------------------------------------------------------- #
EXTRA_READ_DENY_DIRS = [
    ("corpus-lab", "state"),
    ("corpus-lab", "99_scratch"),
    ("corpus-lab", "05_findings"),
    ("REPORT",),
    ("plans_fable",),
    # 2026-09-15: the superseded originals moved out of the root into 00_archive/,
    # ACCEPTANCE.md among them. The whole directory is denied so the oracle stays
    # unreadable whatever it is renamed to, and the explicit file deny below is
    # kept as well -- two independent rules, because this one is the reason the
    # move was safe to make at all.
    ("00_archive",),
]
EXTRA_READ_DENY_FILES = [("00_archive", "ACCEPTANCE.md")]
EXTRA_BASH_DENY = [
    "Bash(*c_card_sample*)",
    "Bash(*frozen20_key*)",
    "Bash(*99_scratch*)",
    "Bash(*stage6_2*)",
    "Bash(*FINDINGS_LIVE*)",
]


def _both_slash_forms(p, suffix):
    """stack._abs_read_denies writes each absolute deny twice, once with
    forward slashes and once with backslashes, because the matcher does not
    normalise them. Same treatment here."""
    fwd = str(p).replace("\\", "/").rstrip("/")
    back = str(p).rstrip("\\")
    return ["Read({}{})".format(fwd, "/" + suffix if suffix else ""),
            "Read({}{})".format(back, "\\" + suffix if suffix else "")]


def deny_list():
    root = L.RETRIEVAL_LAB
    deny = list(STACK.BACKSTOP_DENY)
    for parts in EXTRA_READ_DENY_DIRS:
        for d in _both_slash_forms(root.joinpath(*parts), "**"):
            if d not in deny:
                deny.append(d)
    for parts in EXTRA_READ_DENY_FILES:
        for d in _both_slash_forms(root.joinpath(*parts), ""):
            if d not in deny:
                deny.append(d)
    for b in EXTRA_BASH_DENY:
        if b not in deny:
            deny.append(b)
    return deny


def state_path(root):
    return L.SCRATCH / ("c_stack_state__%s.json" % STACK.corpus_key(root))


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------- #
def do_setup(corpus, py_exe=None, shelf_cli=None, register=True, shelf_dir=None,
             db_path=None, log_dir=None):
    root = Path(corpus).resolve()
    if not root.is_dir():
        print("NO_SUCH_CORPUS %s" % root, file=sys.stderr)
        return 2
    md, cdir = root / "CLAUDE.md", root / ".claude"
    if md.exists() or cdir.exists():
        print("ANOTHER_STACK_PRESENT %s" % root, file=sys.stderr)
        return 3

    py_exe = py_exe or sys.executable
    shelf_cli = shelf_cli or (L.BIN / "c_shelf.py")
    sj = cdir / "settings.json"

    sp = state_path(root)
    sp.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "stack": "s7_shelf", "corpus": str(root), "corpus_key": STACK.corpus_key(root),
        "created": [str(sj), str(md)],
        "created_claude_dir": not cdir.exists(),
        "frozen": {"fusion": FROZEN_FUSION, "caption_channel": FROZEN_CAPTION_CHANNEL,
                   "page_caption": FROZEN_PAGE_CAPTION},
        "installed_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "installing",
    }
    sp.write_text(json.dumps(state, indent=1), encoding="utf-8")

    cdir.mkdir(parents=True, exist_ok=True)
    # F-25: with --log-dir the hooks log there (the install's artefacts) instead of under
    # RUNS, which in a portable layout is a `_private` directory beside the recipient's folder.
    log_arg = ' --log-dir "{}"'.format(Path(log_dir).resolve()) if log_dir else ""
    # The citation guard (F61) rides alongside the deny list: a Stop hook that
    # reads the finished answer and asks once if it cites a page the session
    # never opened. stack.py's hook entries have this shape.
    cfg = {
        "permissions": {"deny": deny_list()},
        # Phase 9.3.3: `--v2` turns on guard v2 (prose page numbers, the Sources
        # block, and figures with no citation at all). Passed as an argument, not
        # an env prefix: a hook command is run by whichever shell the host picks,
        # and `set VAR=1 &&` means different things to cmd.exe and to bash, so an
        # env prefix could switch the guard off without saying so. The guard
        # still defaults to v1, so every recorded P5-P8 number reproduces by
        # running it without the flag.
        "hooks": {"Stop": [{"matcher": "", "hooks": [
            {"type": "command",
             "command": '"{}" "{}" --v2'.format(py_exe, L.BIN / "c_stop_guard.py") + log_arg},
            # The second guard checks what the first cannot: whether the quoted lines are
            # really on the pages they cite, and whether the answer's figures appear in the
            # notes at all. Both ask at most once per session and neither blocks an answer
            # that cited nothing, so a session that disagrees still terminates.
            {"type": "command",
             "command": '"{}" "{}"'.format(py_exe, L.BIN / "c_verify_guard.py") + log_arg}]}]},
    }
    sj.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    md.write_text(claude_md_text(py_exe, shelf_cli), encoding="utf-8")

    state["status"] = "installed"
    state["sha256"] = {str(sj): _sha(sj), str(md): _sha(md)}
    sp.write_text(json.dumps(state, indent=1), encoding="utf-8")

    if register:
        # Phase 9.2.3: --shelf-dir, default unchanged. The registry entry for a
        # rung is only ever rewritten HERE, never by hand, so what the model
        # resolves is always what the installer was told to install.
        # Phase 11.D, finding F-02: --db, same shape as --shelf-dir above and
        # the same default. Before this the db argument was the hardcoded
        # development rung `s2_fts5/harness_15000.db`, which does not exist in a
        # portable install -- so the first command CLAUDE.md prints died with
        # `unable to open database file`. Gate R2 missed it because
        # p10_cleanroom.py passes --db and --shelf explicitly and never resolves
        # through the registry; a real user, and the model, pass neither.
        import c_shelf as CSH
        sd = Path(shelf_dir) if shelf_dir else (L.STACKS / "s7_shelf")
        dbp = Path(db_path) if db_path else (L.STACKS / "s2_fts5" / "harness_15000.db")
        CSH.do_register(str(root), str(dbp), str(sd / "shelf.db"))
        state["shelf_dir"] = str(sd)
        state["db"] = str(dbp)
        sp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    print("INSTALLED %s" % root)
    return 0


def do_status(corpus):
    root = Path(corpus).resolve()
    md, cdir = root / "CLAUDE.md", root / ".claude"
    sj = cdir / "settings.json"
    sp = state_path(root)
    out = {"corpus": str(root),
           "CLAUDE.md": md.exists(), ".claude/": cdir.exists(),
           ".claude/settings.json": sj.exists(), "state_file": sp.exists()}
    print(json.dumps(out, indent=1))
    return 0


def _looks_like_ours(root):
    """Move-proof ownership test: match content, not path.

    The state file lives in the pack's scratch dir keyed by the corpus's ABSOLUTE
    path, so a folder that has been moved has no state file under its new key --
    and do_teardown then refused to remove the two files this tool had written
    itself, calling them foreign. Ownership has to survive a move, so when there is
    no state file the two files are checked for the shape this pack writes: the
    generated CLAUDE.md's first line, and a settings.json that points at our stop
    guard. Anything else is still refused.
    """
    md, sj = root / "CLAUDE.md", root / ".claude" / "settings.json"
    if not md.exists():
        return False
    try:
        if not md.read_text(encoding="utf-8", errors="replace").startswith(CLAUDE_MD_TITLE):
            return False
    except OSError:
        return False
    if sj.exists():
        try:
            cfg = json.loads(sj.read_text(encoding="utf-8"))
        except Exception:
            return False
        if "c_stop_guard.py" not in json.dumps(cfg):
            return False
    return True


def do_teardown(corpus):
    root = Path(corpus).resolve()
    sp = state_path(root)
    md, cdir = root / "CLAUDE.md", root / ".claude"
    sj = cdir / "settings.json"

    if not sp.exists():
        if md.exists() or cdir.exists():
            if not _looks_like_ours(root):
                print("FOREIGN_FILES_PRESENT no state file, refusing to delete %s"
                      % root, file=sys.stderr)
                return 3
            # No state file, but these are demonstrably the files this pack writes:
            # the folder was moved, which is exactly what the pack promises to
            # survive. Remove them and say which files went.
            print("ADOPTED_MOVED_INSTALL %s (no state file for this path)" % root)
            for p in (sj, md):
                if p.exists():
                    p.unlink()
            if cdir.exists():
                try:
                    cdir.rmdir()
                except OSError:
                    print("CLAUDE_DIR_NOT_EMPTY %s" % cdir, file=sys.stderr)
            print("REMOVED %s" % root)
            return 0
        print("ALREADY_CLEAN %s" % root)
        return 0

    state = json.loads(sp.read_text(encoding="utf-8"))
    # `created` is the only source of ownership for a state file that was found. It
    # holds absolute paths, which is correct here: the file was located by a key
    # derived from this corpus's own path, so the paths in it are this corpus's.
    # (A moved folder has no state file at all, which is the branch above.)
    created = {Path(p) for p in state.get("created", [])}
    present = {p for p in (sj, md) if p.exists()}
    foreign = present - created
    if foreign:
        print("FOREIGN_FILES_PRESENT %s" % sorted(str(f) for f in foreign),
              file=sys.stderr)
        return 3
    for p in sorted(created, key=lambda x: len(str(x)), reverse=True):
        if p.exists():
            p.unlink()
    if state.get("created_claude_dir") and cdir.exists():
        try:
            cdir.rmdir()
        except OSError:
            print("CLAUDE_DIR_NOT_EMPTY %s" % cdir, file=sys.stderr)
    state["status"] = "removed"
    state["removed_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    sp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    if md.exists() or cdir.exists():
        print("TEARDOWN_INCOMPLETE %s" % root, file=sys.stderr)
        return 3
    print("REMOVED %s" % root)
    return 0


# --------------------------------------------------------------------- #
# round-trip self test (Stage 4.3)
# --------------------------------------------------------------------- #
def do_selftest():
    base = L.SCRATCH / "c_roundtrip"
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    checks, ok = [], True

    def chk(name, cond):
        nonlocal ok
        checks.append({"check": name, "pass": bool(cond)})
        if not cond:
            ok = False

    md, cdir = base / "CLAUDE.md", base / ".claude"
    sj = cdir / "settings.json"
    sp = state_path(base)

    rc = do_setup(str(base), register=False)
    chk("setup_exit_0", rc == 0)
    chk("claude_md_created", md.exists())
    chk("settings_created", sj.exists())
    chk("state_file_created", sp.exists())
    if sj.exists():
        cfg = json.loads(sj.read_text(encoding="utf-8"))
        deny = cfg.get("permissions", {}).get("deny", [])
        chk("deny_includes_backstop",
            all(d in deny for d in STACK.BACKSTOP_DENY))
        chk("deny_includes_extra_bash",
            all(d in deny for d in EXTRA_BASH_DENY))
        chk("deny_nonempty", len(deny) > len(STACK.BACKSTOP_DENY))
        hooks = cfg.get("hooks", {}).get("Stop", [])
        chk("stop_guard_installed",
            bool(hooks) and "c_stop_guard.py" in json.dumps(hooks))
    if md.exists():
        text = md.read_text(encoding="utf-8")
        chk("claude_md_has_open_before_you_cite", "## 3. Open before you cite" in text)
        chk("claude_md_has_search_result_sentence",
            "is a search result, not evidence" in text)
        chk("claude_md_find_has_frozen_flags",
            ("--fusion %s" % FROZEN_FUSION) in text)
        chk("claude_md_find_has_rewrites", text.count('--q "<rewrite>"') >= 3)
        chk("claude_md_inside_no_stale_caption_flag",
            "--caption " not in text and "--caption dense" not in text)
        chk("claude_md_note_enforcement_sentence",
            "note will refuse a line whose page you have not opened" in text)
        chk("claude_md_no_placeholders",
            "{PY}" not in text and "{SHELF}" not in text)
        # Phase 9.3.2. The three CLAUDE.md v2 additions: the model is shown forty
        # candidates instead of twelve (the Experiment H mechanism, F60/F63), it
        # is told that a year's figure is revised in later editions, and every
        # answer must end in a Sources block naming the page each figure came from.
        chk("claude_md_find_has_compact_40", "--compact 40" in text)
        chk("claude_md_has_vintage_rule",
            "revised, in the next one or two" in text)
        chk("claude_md_has_sources_block_rule",
            "End every answer with a `Sources` block" in text)
        # Test 2 (2026-10-10)
        chk("claude_md_prose_before_absence", "the earliest I could find" in text
            and 'rewrites that pair the row name' in text)
        chk("claude_md_no_evidence_in_own_voice", "Do not describe how you searched" in text)
        chk("claude_md_names_banned_vocabulary", '"shelf", "rewrites"' in text)

    rc2 = do_setup(str(base), register=False)
    chk("second_setup_refuses_exit_3", rc2 == 3)

    rc3 = do_teardown(str(base))
    chk("teardown_exit_0", rc3 == 0)
    chk("claude_md_gone", not md.exists())
    chk("claude_dir_gone", not cdir.exists())

    # stray-file refusal: a CLAUDE.md this script did not create is not deleted
    do_setup(str(base), register=False)
    md.unlink()
    md.write_text("not ours\n", encoding="utf-8")
    state = json.loads(sp.read_text(encoding="utf-8"))
    state["created"] = [str(sj)]          # pretend we never created CLAUDE.md
    sp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    rc4 = do_teardown(str(base))
    chk("stray_file_refused_exit_3", rc4 == 3)
    chk("stray_file_survived", md.exists() and md.read_text(encoding="utf-8") == "not ours\n")

    md.unlink()
    state["created"] = [str(sj), str(md)]
    sp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    do_teardown(str(base))
    shutil.rmtree(base, ignore_errors=True)
    chk("scratch_cleaned", not base.exists())

    rec = {"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "n_checks": len(checks), "n_passed": sum(1 for c in checks if c["pass"]),
           "all_pass": ok, "checks": checks}
    (L.STATE / "c_stack_roundtrip.json").write_text(json.dumps(rec, indent=1),
                                                    encoding="utf-8")
    print(json.dumps({k: v for k, v in rec.items() if k != "checks"}, indent=1))
    for c in checks:
        if not c["pass"]:
            print("FAILED: %s" % c["check"])
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(prog="c_stack.py")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("setup", "status", "teardown"):
        p = sub.add_parser(name)
        p.add_argument("--corpus", required=True)
        if name == "setup":
            p.add_argument("--shelf-dir", dest="shelf_dir", default=None,
                           help="shelf directory to register (default: s7_shelf)")
            p.add_argument("--db", dest="db_path", default=None,
                           help="pages database to register (default: the "
                                "s2_fts5 development rung)")
            p.add_argument("--log-dir", dest="log_dir", default=None,
                           help="where the Stop hooks write their logs (default: RUNS/hooks)")
    sub.add_parser("selftest")
    a = ap.parse_args()
    if not a.cmd:
        ap.print_usage()
        return 2
    if a.cmd == "setup":
        return do_setup(a.corpus, shelf_dir=a.shelf_dir, db_path=a.db_path,
                        log_dir=a.log_dir)
    if a.cmd == "status":
        return do_status(a.corpus)
    if a.cmd == "teardown":
        return do_teardown(a.corpus)
    if a.cmd == "selftest":
        return do_selftest()
    ap.print_usage()
    return 2


if __name__ == "__main__":
    sys.exit(main())
