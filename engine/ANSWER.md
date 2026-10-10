# How to answer questions from this research folder

Built-in Grep and Glob miss most of this tree (PDFs, spreadsheets, scanned files). Do
not rely on them. This folder has a shelf: one card per document saying what
publication it is, which year's edition, and which tables it lists. Work in two moves:
choose the documents from the shelf, then search inside them. Never search all pages at
once.

## 1. Find the publication on the shelf

    "engine/.venv/Scripts/python.exe" "engine/bin/c_shelf.py" find "<the question as asked>" --fusion rrf --caption-channel lex --compact 40 --q "<rewrite>" --q "<rewrite>" --q "<rewrite>"

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

    "engine/.venv/Scripts/python.exe" "engine/bin/c_shelf.py" have "<publication words>" --fy <year like 2012-13>

If it prints NO_EDITION_FOR or NO_FAMILY_MATCHES, the answer is that we do not hold
that document; report the editions that are held and stop. Do not substitute another
year's edition without saying so.

## 2. Find the page inside the document

    "engine/.venv/Scripts/python.exe" "engine/bin/c_shelf.py" inside "<path exactly as printed>" "<the subject in a few words>"
    "engine/.venv/Scripts/python.exe" "engine/bin/c_shelf.py" tables "<path>" --grep "<word>"
    "engine/.venv/Scripts/python.exe" "engine/bin/c_shelf.py" table "<path>" --page <page_index>

tables lists the captions the index found. When the question is the value in a particular
CELL, `table` reads that page's grid from the PDF itself, cell by cell, which `open` cannot
do (the text layer runs a table's columns together). It needs the PDF on disk, and its cells
are NOT citable: a cell read from the layout may be absent from the index's text layer, and
the note guard checks quotes against that text. So use `table` to find the cell and its
neighbours, then open the page and quote the line you actually cite.

For a question about how something changed over years, walk the editions in one go:

    "engine/.venv/Scripts/python.exe" "engine/bin/c_shelf.py" series "<the table row in a few words>" --family "<publication words>" --from 2015-16

series shows you the matching line per edition. It does not open pages; you still must
open the page in each edition you use.

The figure for a fiscal year is usually printed again, revised, in the next one or two
editions. When a question names a year, check the edition for that year and the one or
two after it; report which edition and vintage (provisional, revised, final) each figure
comes from, and if they disagree show both.

## 3. Open before you cite

    "engine/.venv/Scripts/python.exe" "engine/bin/c_shelf.py" open "<path>" <page_index> --slug <slug>

A line printed by find, inside, series or tables is a search result, not evidence. You
may cite a file and page only after you have opened that page with open and quoted the
supporting line or cell in a note.

page_index is the PDF's own page number: a document's first page is p1, so
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

    "engine/.venv/Scripts/python.exe" "engine/bin/c_shelf.py" exact "<the literal string>"

## 4. Save what you find, then answer only from the notes

After opening a useful page:

    "engine/.venv/Scripts/python.exe" "engine/bin/c_shelf.py" note --slug <slug> "<the exact line or table cell, verbatim> | <path> | p<page_index>"

note will refuse a line whose page you have not opened with open under the same slug; if it
refuses, open the page first. It also refuses a quote that does not occur on the page it
cites (NOTE_REFUSED_QUOTE_NOT_ON_PAGE), so copy the line from the page text you just
printed rather than writing it from memory.

Then print and answer from them:

    "engine/.venv/Scripts/python.exe" "engine/bin/c_shelf.py" notes --slug <slug>

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

    "engine/.venv/Scripts/python.exe" "engine/bin/c_shelf.py" verify --slug <slug> --answer <path to your answer>

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

    "engine/.venv/Scripts/python.exe" "engine/bin/c_shelf.py" copies "<path>"

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

## 7. Where your working files go

While the research assistant is on you may not create or change any file in this folder
outside `engine/`; the hooks refuse it. Write the draft answer that `verify --answer` reads
to `engine/99_scratch/<slug>_answer.md`, and nothing else anywhere. Every command above is
run from this folder with the relative paths exactly as printed.

Every reply while the research assistant is on -- an answer, a one-line status, a refusal --
begins with the line `(research assistant on)`. The final check sends back a reply that
does not.
