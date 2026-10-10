#!/usr/bin/env python
"""c_index_selftest.py -- asserts for the index-time logic that is easy to get wrong.

Covers only what has no other test and can be checked without a corpus or a build:
the junk-page detector (a CID-obfuscated page must not be mistaken for readable text)
and the timeout budget (it must have a floor, scale with file size, and stop at a
ceiling). Both are the difference between a page that is silently garbage and a page
that gets read.

    python engine/bin/c_index_selftest.py     # exit 0 pass, 1 fail
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import index_build as IB  # noqa: E402

# A real page of prose, and the same length of what pdftotext emits for a CID font.
PROSE = ("Clause 6 - Price variation shall be calculated on the rate prevailing during the "
         "period when the material was brought to site, and paid after actual consumption.")
MOJIBAKE = (")\x03\x15\x10\x19\x12\x1b\x18\x0f\x03GDWHG\x03\x14\x19\x11\x13\x16\x11\x14\x1b"
            "\x03 3D\\PHQWV\x03IRU\x03(DUWKZRUN\x03\x14\x03\x15\x10\x16\x03 52 7HFK\x03\x0f"
            "\x03\x15\x10\x19\x12\x1b\x18\x0f\x03GDWHG\x03\x14\x19\x11\x13\x16\x11\x14\x1b")


def main():
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))

    # --- junk detection -----------------------------------------------------
    check("prose_is_not_junk", not IB._looks_like_junk(PROSE))
    check("mojibake_is_junk", IB._looks_like_junk(MOJIBAKE))
    check("empty_is_not_junk", not IB._looks_like_junk(""))
    check("newlines_and_tabs_are_not_junk",
          not IB._looks_like_junk("a\nb\tc\rd\n" * 40))
    check("three_stray_control_chars_are_tolerated",
          not IB._looks_like_junk(PROSE + "\x03\x04\x05"))
    check("private_use_area_is_junk",
          IB._looks_like_junk("\ue001" * 20 + "some words here to pad it out"))

    # --- the pack's own files are not corpus material ------------------------
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "doc.pdf").write_bytes(b"%PDF-1.4 tiny")
        (d / "CLAUDE.md").write_text("# How to answer questions from this research folder\n")
        (d / ".claude").mkdir()
        (d / ".claude" / "settings.json").write_text("{}")
        (d / "sub").mkdir()
        (d / "sub" / "CLAUDE.md").write_text("a real document that happens to be named CLAUDE.md")
        keep, excluded = IB.walk(str(d))
        rels = sorted(r for _, r in keep)
        check("root_claude_md_excluded", "CLAUDE.md" not in rels)
        check("dot_claude_excluded", not any(r.startswith(".claude/") for r in rels))
        check("nested_claude_md_still_indexed", "sub/CLAUDE.md" in rels)
        check("ordinary_document_kept", "doc.pdf" in rels)
        check("tool_owned_files_counted_as_excluded", excluded == 2)

    # --- timeout budget -----------------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        small = os.path.join(td, "small.bin")
        open(small, "wb").write(b"x" * 200)
        check("tiny_file_gets_the_floor", IB.pdf_timeout(small) == IB.PDF_TIMEOUT)

        big = os.path.join(td, "big.bin")          # sparse: instant, no disk use
        with open(big, "wb") as fh:
            fh.truncate(100 * 1024 * 1024)
        t100 = IB.pdf_timeout(big)
        check("100MB_scales_above_the_floor", t100 > IB.PDF_TIMEOUT)
        check("100MB_below_the_ceiling", t100 < IB.PDF_TIMEOUT_MAX)

        huge = os.path.join(td, "huge.bin")
        with open(huge, "wb") as fh:
            fh.truncate(400 * 1024 * 1024)
        check("400MB_is_capped", IB.pdf_timeout(huge) == IB.PDF_TIMEOUT_MAX)

    n_pass = sum(1 for _, ok in checks if ok)
    for name, ok in checks:
        print("%-42s %s" % (name, "pass" if ok else "FAIL"))
    print("index logic: %d/%d pass" % (n_pass, len(checks)))
    return 0 if n_pass == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
