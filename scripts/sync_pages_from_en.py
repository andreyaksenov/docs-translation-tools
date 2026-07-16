#!/usr/bin/env python3
"""
Sync a RU page's structure/content with its EN counterpart after an EN edit.

Never touches the EN file. Aligns the RU file's structural "skeleton"
(headings, anchors, delimited blocks, option/flag terms, code lines) to EN's,
and copies in new or changed EN lines verbatim (left untranslated) wherever
RU has nothing corresponding yet. Existing RU prose is never rewritten or
removed -- only technical tokens that must be byte-identical across
languages (flag names, code/command lines, include paths, ids) are corrected
when they've drifted (e.g. a stale `plpythonu` left behind after EN moved to
`plpython3u`).

Usage:
    scripts/sync_pages_from_en.py <path/to/en/.../file.adoc> [-n|--dry-run]
"""
import argparse
import difflib
import re
import sys
from pathlib import Path

EN_MARK = "en/modules/ROOT"
RU_MARK = "ru/modules/ROOT"

# Delimited-block fences: two-dash open block ('--'), 4+ dashes/dots/equals/
# asterisks (listing/literal/example/sidebar), 6-equals ([tabs] blocks), and
# table fences ('|===').
DELIM_RE = re.compile(r'^(?:-{2}|-{4,}|\.{4,}|={4,}|\*{4,}|\|={3,})$')
# Only listing/literal blocks are "code" (exact-match) content; open blocks,
# example/admonition/sidebar/tabs/table blocks contain ordinary translated
# prose or definition-list terms and use the normal top-level classifier.
CODE_DELIM_RE = re.compile(r'^(?:-{4,}|\.{4,})$')

HEADING_RE = re.compile(r'^(=+)\s+\S')
ID_RE = re.compile(r'^\[#([\w-]+)]\s*$')
DOCATTR_RE = re.compile(r'^:([\w-]+):')
ATTR_RE = re.compile(r'^\[[^\[\]].*]\s*$')
INCLUDE_RE = re.compile(r'^include::')
BLOCKTITLE_RE = re.compile(r'^\.[^.\s]')
TERM_RE = re.compile(r'^(\[\[[\w-]+])?(.+)::\s*$')
COMMENT_IN_CODE_RE = re.compile(r'^\s*(#|--|//)\s')
CYRILLIC_RE = re.compile(r'[Ѐ-ӿ]')

# Signature types whose text is required to be byte-identical between EN and
# RU (flag names, code, paths, ids). When a 'replace' op pairs an EN/RU line
# of one of these types and the text differs, RU is overwritten with EN's.
FORCE_SYNC_TYPES = {"DELIM", "ID", "ATTR", "INCLUDE", "TERM", "CODE", "CONT"}


def classify(line, stack):
    stripped = line.strip()

    if DELIM_RE.match(stripped):
        if stack and stack[-1] == stripped:
            stack.pop()
        else:
            stack.append(stripped)
        return ("DELIM", stripped)

    if stack and CODE_DELIM_RE.match(stack[-1]):
        if stripped == "":
            return ("BLANK",)
        if COMMENT_IN_CODE_RE.match(line):
            return ("COMMENT",)
        return ("CODE", line)

    if stripped == "":
        return ("BLANK",)

    if stripped == "+":
        return ("CONT", "+")

    m = HEADING_RE.match(line)
    if m:
        return ("HEADING", len(m.group(1)))

    m = ID_RE.match(line)
    if m:
        return ("ID", m.group(1))

    m = DOCATTR_RE.match(line)
    if m:
        return ("DOCATTR", m.group(1))

    if ATTR_RE.match(line):
        return ("ATTR", line)

    if INCLUDE_RE.match(line):
        return ("INCLUDE", line)

    if BLOCKTITLE_RE.match(line):
        return ("BLOCKTITLE",)

    m = TERM_RE.match(line)
    if m:
        content = m.group(2)
        # Only flag-like ('-x', '--long-flag') or code-span (`name`) terms are
        # untranslatable; a plain word/phrase term (e.g. a [tabs] tab label
        # like "Day name::") is ordinary translated content, not a literal.
        if content.lstrip().startswith(("-", "`")):
            return ("TERM", line)
        return ("TERMX",)

    return ("PROSE",)


def signatures(lines):
    stack = []
    return [classify(line, stack) for line in lines]


# Types whose text is genuinely comparable/translated content, not a literal
# anchor: two lines with the same generic tag carry no guarantee of actually
# corresponding to each other (they're just "some prose", "some heading").
# Letting SequenceMatcher treat them as interchangeable lets it "cheat" and
# match e.g. EN sentence 2 against RU sentence 1 of a paragraph, which then
# misplaces a newly-added sentence at the front instead of the end. Making
# each such line's matching signature unique-per-side forces SequenceMatcher
# to only ever bracket them between real (exact-match) anchors, and the
# 'replace' handling in merge() then aligns them from the front by position.
GENERIC_TYPES = {"PROSE", "COMMENT", "HEADING", "BLOCKTITLE", "TERMX"}


def matching_signatures(sigs, side):
    out = []
    for idx, sig in enumerate(sigs):
        if sig[0] in GENERIC_TYPES:
            out.append((sig[0], side, idx))
        else:
            out.append(sig)
    return out


def _sync_pair(en_l, ru_l, sig_type, replaced):
    # A Cyrillic RU line is clearly deliberate translated content (a
    # label/comment inside a literal block, say), never stale code left
    # behind from an old EN wording -- never overwrite it.
    if sig_type in FORCE_SYNC_TYPES and en_l != ru_l and not CYRILLIC_RE.search(ru_l):
        replaced.append((ru_l, en_l))
        return en_l
    return ru_l


def _front_pair_and_append(en_slice, ru_slice, sig_slice, out, inserted, replaced):
    common = min(len(en_slice), len(ru_slice))
    for k in range(common):
        out.append(_sync_pair(en_slice[k], ru_slice[k], sig_slice[k][0], replaced))
    if len(en_slice) > common:
        extra = en_slice[common:]
        out.extend(extra)
        inserted.append(extra)
    elif len(ru_slice) > common:
        out.extend(ru_slice[common:])


def _align_replace_span(en_slice, ru_slice, en_sig_slice, ru_sig_slice, out, inserted, replaced):
    # A naive positional zip assumes the k-th EN line always corresponds to
    # the k-th RU line, which breaks when the two sides' *kinds* of content
    # don't actually line up here (e.g. RU is missing a blank line that EN
    # has before a block title): zipping raw position would then pair a
    # BLANK against a BLOCKTITLE, silently drop the blank, and strand the
    # title as a spurious duplicate insert. Aligning by type first (nested
    # diff) fixes that: same-type runs still pair front-to-back as before,
    # but a type that only exists on one side becomes a clean insert/leave-
    # as-is instead of a bad pairing.
    en_types = [t[0] for t in en_sig_slice]
    ru_types = [t[0] for t in ru_sig_slice]
    sm2 = difflib.SequenceMatcher(a=en_types, b=ru_types, autojunk=False)

    for tag, i1, i2, j1, j2 in sm2.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                out.append(_sync_pair(en_slice[i1 + k], ru_slice[j1 + k], en_sig_slice[i1 + k][0], replaced))
        elif tag == "delete":
            new_lines = en_slice[i1:i2]
            out.extend(new_lines)
            inserted.append(new_lines)
        elif tag == "insert":
            out.extend(ru_slice[j1:j2])
        elif tag == "replace":
            _front_pair_and_append(en_slice[i1:i2], ru_slice[j1:j2], en_sig_slice[i1:i2], out, inserted, replaced)


def merge(en_lines, ru_lines):
    en_sigs = signatures(en_lines)
    ru_sigs = signatures(ru_lines)
    en_match = matching_signatures(en_sigs, "EN")
    ru_match = matching_signatures(ru_sigs, "RU")
    sm = difflib.SequenceMatcher(a=en_match, b=ru_match, autojunk=False)

    out = []
    inserted = []   # list[list[str]]
    replaced = []   # list[(old, new)]

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.extend(ru_lines[j1:j2])
        elif tag == "delete":
            # a[i1:i2] (EN) has no counterpart in b (RU): new EN content.
            new_lines = en_lines[i1:i2]
            out.extend(new_lines)
            inserted.append(new_lines)
        elif tag == "insert":
            # b[j1:j2] (RU) has no counterpart in a (EN): leave RU as-is.
            out.extend(ru_lines[j1:j2])
        elif tag == "replace":
            _align_replace_span(
                en_lines[i1:i2], ru_lines[j1:j2], en_sigs[i1:i2], ru_sigs[j1:j2],
                out, inserted, replaced,
            )

    return out, inserted, replaced


def ru_path_for(en_path: Path) -> Path:
    s = str(en_path)
    if EN_MARK not in s:
        sys.exit(f"error: path does not look like an EN page (missing '{EN_MARK}'): {en_path}")
    return Path(s.replace(EN_MARK, RU_MARK, 1))


def read_lines(path: Path):
    return path.read_text(encoding="utf-8").splitlines()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("en_file", help="path to the EN .adoc file that was updated")
    parser.add_argument("-n", "--dry-run", action="store_true", help="print a diff instead of writing the RU file")
    args = parser.parse_args()

    en_path = Path(args.en_file)
    if not en_path.is_file():
        sys.exit(f"error: not a file: {en_path}")

    ru_path = ru_path_for(en_path)
    en_lines = read_lines(en_path)

    if ru_path.is_file():
        ru_lines = read_lines(ru_path)
    else:
        print(f"NOTE: {ru_path} does not exist yet -- creating it as a full (untranslated) copy of EN.")
        ru_lines = []

    merged, inserted, replaced = merge(en_lines, ru_lines)

    if merged == ru_lines:
        print(f"OK: {ru_path} already matches the EN structure/content; nothing to do.")
        return

    if args.dry_run:
        diff = difflib.unified_diff(
            ru_lines, merged,
            fromfile=str(ru_path), tofile=str(ru_path) + " (proposed)",
            lineterm="",
        )
        print("\n".join(diff))
    else:
        ru_path.parent.mkdir(parents=True, exist_ok=True)
        ru_path.write_text("\n".join(merged) + "\n", encoding="utf-8")
        print(f"Updated {ru_path}")

    if inserted:
        total = sum(len(b) for b in inserted)
        print(f"\nInserted {total} new line(s) from EN across {len(inserted)} block(s), left untranslated:")
        for block in inserted:
            for l in block:
                print(f"  + {l}")
            print()

    if replaced:
        print(f"Synced {len(replaced)} stale technical line(s) (flags/code/ids/paths) to match EN:")
        for old, new in replaced:
            print(f"  - {old}")
            print(f"  + {new}")

    print("\nNext: run scripts/check_pages_translation.sh to locate the newly untranslated lines for translation.")


if __name__ == "__main__":
    main()