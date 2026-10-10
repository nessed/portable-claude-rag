#!/usr/bin/env python
"""c_note_quote_selftest.py -- asserts for the note quote guard.

The guard in c_shelf.do_note is the thing that stops an invented line being
recorded as a citation, so it is the one piece of this package that must not be
trusted without a test. It needs no index and no corpus: the quote logic is pure
(text in, verdict out), so this runs in under a second anywhere.

    python engine/bin/c_note_quote_selftest.py     # exit 0 pass, 1 fail

Checks, in the order they matter:
  1. a quote that IS on the page passes, whatever the whitespace
  2. a quote that is NOT on the page is refused, and names the chunk that failed
  3. an elided quote ("a ... b") requires BOTH ends, not just one
  4. a numeric chunk as short as three characters is still checked (an invented
     figure is exactly what this guard exists for)
  5. a two-character chunk is below the floor and is not checked (it would match
     noise) -- the documented ceiling of the check
  6. the citation tail is parsed from the same shape CLAUDE.md prescribes
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import c_shelf as CS  # noqa: E402

PAGE = """GOVERNMENT OF THE PUNJAB
             ANNUAL BUDGET STATEMENT
     Table 4: Provincial Excise Revenue Target
     (Rupees in million)
     Province       2025-26        2026-27
     Punjab            18,420         21,375
     Estimated provincial excise revenue target for 2026-27 is 21,375.
"""


def main():
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))

    # 1. present, whitespace-insensitive (the page is column-laid-out)
    ok, missing = CS._quote_on_page("Estimated provincial excise revenue target for 2026-27", PAGE)
    check("quote_on_page_passes", ok and missing is None)
    ok, _ = CS._quote_on_page("Province  2025-26  2026-27", PAGE)
    check("quote_on_page_ignores_whitespace", ok)
    ok, _ = CS._quote_on_page("PROVINCE 2025-26 2026-27", PAGE)
    check("quote_on_page_ignores_case", ok)

    # 2. absent -> refused, and the failing chunk is named
    ok, missing = CS._quote_on_page("Excise revenue target is 44,999", PAGE)
    check("invented_quote_refused", not ok)
    # With no elision mark the whole quote is one chunk, so that is what is named
    # as absent -- the check does not try to guess which word was invented.
    check("invented_quote_names_missing_chunk",
          missing == "excise revenue target is 44,999")

    # 3. elision: both ends required
    ok, _ = CS._quote_on_page("GOVERNMENT OF THE PUNJAB ... 21,375", PAGE)
    check("elided_quote_both_ends_present_passes", ok)
    ok, _ = CS._quote_on_page("GOVERNMENT OF THE PUNJAB ... 99,999", PAGE)
    check("elided_quote_one_absent_end_refused", not ok)

    # 4. a short numeric chunk is still checked
    check("short_numeric_chunk_kept", CS._quote_chunks("21,375") == ["21,375"])
    check("short_numeric_chunk_absent_refused", not CS._quote_on_page("9,999", PAGE)[0])

    # 5. below the floor: documented ceiling, not a bug
    check("two_char_chunk_below_floor", CS._quote_chunks("42") == [])

    # 6. the citation tail, as CLAUDE.md prescribes it
    m = CS._NOTE_TAIL_RE.search('21,375 | Non-development (Volume - I).pdf | p417')
    check("citation_tail_parsed", bool(m))
    check("citation_tail_path", m and m.group("path").strip() == "Non-development (Volume - I).pdf")
    check("citation_tail_page", m and int(m.group("page")) == 417)
    check("citation_tail_absent_when_no_tail",
          not CS._NOTE_TAIL_RE.search("21,375 is the target"))

    # 7. the answer's figures against the notes' quotes
    notes = [{"quote": "Total Capital Receipts (A+B) 610.456 802.560 498.882"},
             {"quote": "Estimated provincial excise revenue target for 2026-27 is 21,375."}]
    figs = {c["canonical"]: c["supported"]
            for c in CS.check_answer_numbers("Receipts were 610.456 in 2024-25.", notes)}
    check("answer_figure_in_a_quote_supported", figs.get("610.456") is True)
    figs = {c["canonical"]: c["supported"]
            for c in CS.check_answer_numbers("Receipts were 444.999 in 2024-25.", notes)}
    check("answer_figure_absent_flagged", figs.get("444.999") is False)
    # commas are not part of the number: a quote printing 1,234 answers an answer's 1234
    figs = {c["canonical"]: c["supported"]
            for c in CS.check_answer_numbers("Excise was 21375.", notes)}
    check("answer_figure_matches_across_commas", figs.get("21375") is True)
    # a year and a single digit are not claims; a fiscal year's second half is not one
    # either, and the answer's own citation tails are provenance, not claims
    check("year_is_not_a_claim", CS._numbers_in("in 2024-25 receipts rose") == [])
    check("fiscal_year_second_half_is_not_a_claim",
          CS._numbers_in("FY 2026-27 and 2018/19") == [])
    check("single_digit_is_not_a_claim", CS._numbers_in("two of 5 years") == [])
    tail_claims = CS.check_answer_numbers("610.456 | Report 2024.pdf | p12", notes)
    check("citation_tail_is_not_a_claim",
          [c["canonical"] for c in tail_claims] == ["610.456"] and
          tail_claims[0]["supported"] is True)
    # token boundaries: a quote printing 2018-19 does not support the figure 18
    check("no_substring_support",
          CS.check_answer_numbers("18 percent", [{"quote": "FY 2018-19 actuals"}])[0][
              "supported"] is False)
    # a rounded restatement is reported, not silently accepted
    check("rounded_restatement_is_reported",
          CS.check_answer_numbers("about 610.5 million", notes)[0]["supported"] is False)

    n_pass = sum(1 for _, ok in checks if ok)
    for name, ok in checks:
        print("%-46s %s" % (name, "pass" if ok else "FAIL"))
    print("quote guard: %d/%d pass" % (n_pass, len(checks)))
    return 0 if n_pass == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
