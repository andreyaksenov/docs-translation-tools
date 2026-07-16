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
import subprocess
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
ALL_CAPS_TERM_RE = re.compile(r'^[A-Z][A-Z0-9_]*(\s*,\s*[A-Z][A-Z0-9_]*)*$')
# A list item that's just a bare xref (e.g. a "See also" bullet). The target
# path is never translated, so align on it like an anchor -- otherwise a
# newly-inserted bullet is generic "prose" to the aligner, and a naive
# front-to-back positional pairing against the existing (differently-
# ordered) bullets can discard the new item and duplicate an old one instead.
XREF_ITEM_RE = re.compile(r'^[*.]+\s*xref:([^\[]+)\[[^\]]*]\s*$')
# The first cell of a PSV table row whose content is an untranslated literal
# identifier (a config/YAML key name, e.g. `|HOST <coordinator_hostname>` or
# `|VERSION`) -- these are kept byte-identical across EN/RU everywhere in this
# docs set. Without recognizing them, every such line falls through to plain
# PROSE like the surrounding translated cells (`a|...` descriptions, `|Yes`/
# `|Да` markers), and a table of many near-identical (CELL, PROSE, BLANK)
# row shapes gives the nested type-only diff nothing real to anchor on --
# once one row's line count drifts, alignment cascades wrong for the rest of
# the table. Requiring 2+ leading uppercase/digit/underscore chars excludes
# ordinary capitalized English cells like `|Yes` or `|The database ...`
# (which have a lowercase letter right after the initial capital).
CELL_KEY_RE = re.compile(r'^\|[A-Z][A-Z0-9_]+\b')
COMMENT_IN_CODE_RE = re.compile(r'^\s*(#|--|//)\s')
STALE_MARK_RE = re.compile(r'^// STALE VERSION:')
CYRILLIC_RE = re.compile(r'[Ѐ-ӿ]')

# Signature types whose text is required to be byte-identical between EN and
# RU (flag names, code, paths, ids). When a 'replace' op pairs an EN/RU line
# of one of these types and the text differs, RU is overwritten with EN's.
FORCE_SYNC_TYPES = {"DELIM", "ID", "ATTR", "INCLUDE", "TERM", "CODE", "CONT", "CELLKEY"}


def classify(line, stack):
    stripped = line.strip()

    # A stale-marker comment left by a previous sync run. It only ever
    # exists on the RU side and is expected to have no EN counterpart, so it
    # must never be mistaken for ordinary PROSE: doing so lets it steal a
    # pairing slot in the nested type-alignment from a real adjacent line
    # (misplacing that line) and makes it look "orphaned" in its own report.
    if STALE_MARK_RE.match(stripped):
        return ("STALEMARK",)

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

    m = XREF_ITEM_RE.match(line)
    if m:
        return ("XREFITEM", m.group(1))

    if BLOCKTITLE_RE.match(line):
        return ("BLOCKTITLE",)

    # Guard against a translated RU description cell that merely *starts*
    # with a Latin acronym kept untranslated (e.g. `|SQL-команда...`,
    # `|HTTP-запрос...`) -- a genuine literal key cell never contains
    # Cyrillic, so require the whole line to be Cyrillic-free.
    if CELL_KEY_RE.match(line) and not CYRILLIC_RE.search(line):
        return ("CELLKEY", stripped)

    m = TERM_RE.match(line)
    if m:
        content = m.group(2).strip()
        # Flag-like ('-x', '--long-flag'), code-span (`name`), and ALL-CAPS
        # config-file parameter names (PORT_BASE, HBA_HOSTNAMES, possibly a
        # comma-separated list like "QD_PRIMARY_ARRAY, PRIMARY_ARRAY") are all
        # untranslatable identifiers and should align like anchors, not
        # positionally -- otherwise a page full of config-key definitions
        # (gpinitsystem-style) has no real alignment anchors at all, and a
        # structural change elsewhere on the page can drift an unrelated,
        # already-correct key/description pair apart. A plain word/phrase
        # term (e.g. a [tabs] tab label like "Day name::") is ordinary
        # translated content, not a literal.
        if content.startswith(("-", "`")) or ALL_CAPS_TERM_RE.match(content):
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


def _sync_pair(en_l, ru_l, sig_type, replaced, en_idx, force_synced):
    # A Cyrillic RU line is clearly deliberate translated content (a
    # label/comment inside a literal block, say), never stale code left
    # behind from an old EN wording -- never overwrite it.
    if sig_type in FORCE_SYNC_TYPES and en_l != ru_l and not CYRILLIC_RE.search(ru_l):
        replaced.append((ru_l, en_l))
        force_synced.add(en_idx)
        return en_l
    return ru_l


def _front_pair_and_append(en_slice, ru_slice, en_sig_slice, ru_sig_slice, out, inserted, replaced, pairs, en_base, ru_base, force_synced, orphaned):
    # Independent EN/RU cursors, not a shared index: a STALEMARK line (from
    # this same run's own stale-marking pass, or a leftover from an earlier
    # one) is RU-only *by construction* -- it is never supposed to consume
    # an EN slot or shift the positional pairing of the real content that
    # follows it. Treating it like a generic type mismatch (as a shared-index
    # zip would) misreads the line right after it as "new" and duplicates
    # it, purely because the marker shifted the RU side by one.
    ei = ri = 0
    while ei < len(en_slice) and ri < len(ru_slice):
        if ru_sig_slice[ri][0] == "STALEMARK":
            out.append(ru_slice[ri])
            ri += 1
            continue
        en_type, ru_type = en_sig_slice[ei][0], ru_sig_slice[ri][0]
        if en_type != ru_type:
            # A mismatch here means the nested type-level diff found no
            # anchor to bracket this remainder on (generic types are
            # deliberately incomparable across sides, to avoid misplacing
            # sentences) -- but if the *tail* of the longer remainder has
            # the same type shape as the whole shorter remainder, that's a
            # strong sign the real edit was "new content prepended" (or old
            # content removed from the front), not "everything from here on
            # is unrelated". Peel off just the non-matching prefix as new/
            # orphaned and resume normal pairing from where the shapes line
            # up again -- otherwise a block prepended in EN (e.g. a new
            # admonition) gets torn apart and interleaved one line at a time
            # with the unrelated RU sentences it now sits in front of.
            en_rest_types = [t[0] for t in en_sig_slice[ei:]]
            ru_rest_types = [t[0] for t in ru_sig_slice[ri:]]
            if len(en_rest_types) > len(ru_rest_types) and en_rest_types[-len(ru_rest_types):] == ru_rest_types:
                prefix_len = len(en_rest_types) - len(ru_rest_types)
                extra = en_slice[ei:ei + prefix_len]
                out.extend(extra)
                inserted.append(extra)
                ei += prefix_len
                continue
            if len(ru_rest_types) > len(en_rest_types) and ru_rest_types[-len(en_rest_types):] == en_rest_types:
                prefix_len = len(ru_rest_types) - len(en_rest_types)
                extra = ru_slice[ri:ri + prefix_len]
                out.extend(extra)
                non_marker = [l for l, s in zip(extra, ru_sig_slice[ri:ri + prefix_len]) if s[0] != "STALEMARK"]
                if non_marker:
                    orphaned.append(non_marker)
                ri += prefix_len
                continue
            # No clean suffix alignment either -- genuinely unrelated content
            # on both sides for the rest of this span. Decouple them -- keep
            # RU's line as-is and insert EN's as new -- rather than zipping
            # positionally and silently discarding one side.
            out.append(ru_slice[ri])
            orphaned.append([ru_slice[ri]])
            out.append(en_slice[ei])
            inserted.append([en_slice[ei]])
            ei += 1
            ri += 1
            continue
        out.append(_sync_pair(en_slice[ei], ru_slice[ri], en_type, replaced, en_base + ei, force_synced))
        pairs.append((en_base + ei, ru_base + ri))
        ei += 1
        ri += 1
    if ei < len(en_slice):
        extra = en_slice[ei:]
        out.extend(extra)
        inserted.append(extra)
    if ri < len(ru_slice):
        extra = ru_slice[ri:]
        out.extend(extra)
        non_marker = [l for l, s in zip(extra, ru_sig_slice[ri:]) if s[0] != "STALEMARK"]
        if non_marker:
            orphaned.append(non_marker)


def _align_replace_span(en_slice, ru_slice, en_sig_slice, ru_sig_slice, out, inserted, replaced, pairs, en_base, ru_base, force_synced, orphaned):
    # A naive positional zip assumes the k-th EN line always corresponds to
    # the k-th RU line, which breaks when the two sides' *kinds* of content
    # don't actually line up here (e.g. RU is missing a blank line that EN
    # has before a block title): zipping raw position would then pair a
    # BLANK against a BLOCKTITLE, silently drop the blank, and strand the
    # title as a spurious duplicate insert. Aligning by type first (nested
    # diff) fixes that: same-type runs still pair front-to-back as before,
    # but a type that only exists on one side becomes a clean insert/leave-
    # as-is instead of a bad pairing.
    # Reuse the same per-side-unique wrapping as the top-level matcher: a
    # generic type (PROSE, BLOCKTITLE, ...) reduced to its bare type name is
    # not a trustworthy anchor -- two unrelated PROSE lines both being
    # "PROSE" does not mean they correspond, and a nested 'equal' opcode
    # skips _sync_pair's force-sync guard entirely, silently keeping RU's
    # old text and discarding the new EN line. BLANK is deliberately not a
    # generic type (a blank line carries no content, so treating any blank
    # as interchangeable with any other is safe) -- that's what lets a
    # missing blank line still realign correctly around a real title/anchor.
    en_types = matching_signatures(en_sig_slice, "EN")
    ru_types = matching_signatures(ru_sig_slice, "RU")
    sm2 = difflib.SequenceMatcher(a=en_types, b=ru_types, autojunk=False)

    for tag, i1, i2, j1, j2 in sm2.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                out.append(_sync_pair(
                    en_slice[i1 + k], ru_slice[j1 + k], en_sig_slice[i1 + k][0], replaced,
                    en_base + i1 + k, force_synced,
                    ))
                pairs.append((en_base + i1 + k, ru_base + j1 + k))
        elif tag == "delete":
            new_lines = en_slice[i1:i2]
            out.extend(new_lines)
            inserted.append(new_lines)
        elif tag == "insert":
            extra = ru_slice[j1:j2]
            out.extend(extra)
            orphaned.append(extra)
        elif tag == "replace":
            _front_pair_and_append(
                en_slice[i1:i2], ru_slice[j1:j2], en_sig_slice[i1:i2], ru_sig_slice[j1:j2],
                out, inserted, replaced, pairs, en_base + i1, ru_base + j1, force_synced, orphaned,
                                                )


def merge(en_lines, ru_lines):
    en_sigs = signatures(en_lines)
    ru_sigs = signatures(ru_lines)
    en_match = matching_signatures(en_sigs, "EN")
    ru_match = matching_signatures(ru_sigs, "RU")
    sm = difflib.SequenceMatcher(a=en_match, b=ru_match, autojunk=False)

    out = []
    inserted = []   # list[list[str]]
    replaced = []   # list[(old, new)]
    pairs = []      # list[(en_idx, ru_idx)] -- every position where a 1:1 EN<->RU correspondence was established
    force_synced = set()  # en_idx values already auto-corrected via `replaced` -- not also "possibly stale" prose
    orphaned = []   # list[list[str]] -- RU content structurally left with no EN counterpart at all (never deleted)

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.extend(ru_lines[j1:j2])
            for k in range(i2 - i1):
                pairs.append((i1 + k, j1 + k))
        elif tag == "delete":
            # a[i1:i2] (EN) has no counterpart in b (RU): new EN content.
            new_lines = en_lines[i1:i2]
            out.extend(new_lines)
            inserted.append(new_lines)
        elif tag == "insert":
            # b[j1:j2] (RU) has no counterpart in a (EN): leave RU as-is, but
            # flag it -- this is exactly the shape left behind when EN
            # deliberately removes a whole section (e.g. a bogus option):
            # RU's now-orphaned content has nothing left to align against.
            extra = ru_lines[j1:j2]
            out.extend(extra)
            orphaned.append(extra)
        elif tag == "replace":
            _align_replace_span(
                en_lines[i1:i2], ru_lines[j1:j2], en_sigs[i1:i2], ru_sigs[j1:j2],
                out, inserted, replaced, pairs, i1, j1, force_synced, orphaned,
            )

    return out, inserted, replaced, pairs, force_synced, orphaned


HUNK_RE = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')


def _last_commit_touching(path: Path):
    result = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", str(path)],
        capture_output=True, text=True,
    )
    sha = result.stdout.strip()
    return sha or None


def _git_diff_hunks(ref: str, path: Path):
    # -U0: zero context lines, so each hunk's line ranges map exactly to the
    # lines actually touched by the edit -- no unrelated context to filter out.
    result = subprocess.run(
        ["git", "diff", "--unified=0", ref, "--", str(path)],
        capture_output=True, text=True,
    )
    hunks = []
    current = None
    for line in result.stdout.splitlines():
        m = HUNK_RE.match(line)
        if m:
            if current:
                hunks.append(current)
            old_start, old_count, new_start, new_count = m.groups()
            current = {
                "old_count": int(old_count) if old_count is not None else 1,
                "new_start": int(new_start),
                "new_count": int(new_count) if new_count is not None else 1,
                "minus": [], "plus": [],
            }
        elif current is not None and line.startswith("-") and not line.startswith("---"):
            current["minus"].append(line[1:])
        elif current is not None and line.startswith("+") and not line.startswith("+++"):
            current["plus"].append(line[1:])
    if current:
        hunks.append(current)
    return hunks


def find_reworded_lines(en_path: Path, ru_path: Path, en_lines, ru_lines, pairs, force_synced, since: str = None):
    """Find EN lines that were *modified* (not purely added) since `since`
    (default: the last commit that touched the RU file), and map them to
    their currently-aligned RU line. This catches the case the structural
    merge cannot: an existing sentence reworded in place, where the line
    count doesn't change so the aligner treats it as an unchanged 'equal'
    pair and RU -- though still fully translated-looking text -- silently
    goes stale. Never edits anything; for review only.
    """
    ref = since or _last_commit_touching(ru_path)
    if not ref:
        return None, []

    # A RU line that was itself touched since `ref` -- e.g. hand-translated
    # in the working tree but not committed yet -- has already been dealt
    # with, even though it (correctly) still reads nothing like the EN text.
    # Without this, every real translation would immediately get re-flagged
    # as "stale" purely for not being byte-identical to EN.
    ru_touched = set()
    for h in _git_diff_hunks(ref, ru_path):
        if h["new_count"] == 0:
            continue
        ru_touched.update(range(h["new_start"], h["new_start"] + h["new_count"]))

    hunks = _git_diff_hunks(ref, en_path)
    en_to_ru = dict(pairs)
    findings = []
    for h in hunks:
        if h["old_count"] == 0 or h["new_count"] == 0:
            continue  # pure addition or pure deletion, not a reword
        for k in range(h["new_count"]):
            new_lineno = h["new_start"] + k
            en_idx = new_lineno - 1
            if en_idx in force_synced:
                continue  # already auto-corrected (a literal/code token drift), not a translation gap
            ru_idx = en_to_ru.get(en_idx)
            if ru_idx is None:
                continue  # new/inserted line, not an existing aligned pair -- already handled elsewhere
            if (ru_idx + 1) in ru_touched:
                continue  # RU already edited since ref -- treat as resolved
            old_en = h["minus"][k] if k < len(h["minus"]) else None
            findings.append({
                "lineno": new_lineno,
                "old_en": old_en,
                "new_en": en_lines[en_idx],
                "ru_lineno": ru_idx + 1,
                "ru": ru_lines[ru_idx],
            })
    return ref, findings


def apply_stale_markers(ru_lines, reworded):
    """Replace each reworded RU line with the new EN sentence (left
    untranslated, same as any other new content), preserving the old RU
    wording as a comment on its own line right after -- AsciiDoc only
    treats '//' as a comment when it's the first thing on the line, so it
    can't be appended inline after real paragraph text.
    """
    marked = list(ru_lines)
    count = 0
    # Descending order so inserting a comment line doesn't shift the
    # position of findings at smaller indices not yet applied.
    for f in sorted(reworded, key=lambda f: f["ru_lineno"], reverse=True):
        ru_idx = f["ru_lineno"] - 1
        if marked[ru_idx] == f["new_en"]:
            continue  # already marked on an earlier (uncommitted) run -- idempotent no-op
        old_ru = marked[ru_idx]
        marked[ru_idx] = f["new_en"]
        marked.insert(ru_idx + 1, f"// STALE VERSION: {old_ru}")
        count += 1
    return marked, count


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
    parser.add_argument(
        "--since", metavar="REF",
        help="git ref to diff the EN file against when looking for reworded (not just added) lines "
             "(default: the last commit that touched the RU file)",
    )
    args = parser.parse_args()

    en_path = Path(args.en_file)
    if not en_path.is_file():
        sys.exit(f"error: not a file: {en_path}")

    ru_path = ru_path_for(en_path)
    en_lines = read_lines(en_path)
    ru_existed = ru_path.is_file()

    if ru_existed:
        ru_lines = read_lines(ru_path)
    else:
        print(f"NOTE: {ru_path} does not exist yet -- creating it as a full (untranslated) copy of EN.")
        ru_lines = []

    merged, inserted, replaced, pairs, force_synced, orphaned = merge(en_lines, ru_lines)

    # Structural sync only catches new/missing lines and literal-token drift.
    # An existing sentence that was *reworded* in place (same line count) is
    # invisible to it -- RU still reads as normal, fully-translated text, so
    # nothing here flags it as needing attention without checking git history.
    ref, reworded, marked = None, [], 0
    if ru_existed:
        ref, reworded = find_reworded_lines(en_path, ru_path, en_lines, ru_lines, pairs, force_synced, since=args.since)
        if reworded:
            ru_lines_marked, marked = apply_stale_markers(ru_lines, reworded)
            if marked:
                # Re-run the structural merge over the stale-marked RU lines so the
                # two kinds of changes (new/missing content, reworded-in-place
                # content) land together in a single coherent diff/write.
                merged, inserted, replaced, pairs, force_synced, orphaned = merge(en_lines, ru_lines_marked)

    structurally_synced = merged == ru_lines

    if structurally_synced:
        print(f"OK: {ru_path} already matches the EN structure/content; nothing to do structurally.")
    elif args.dry_run:
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

    if marked:
        print(f"\nMarked {marked} reworded line(s) (EN wording changed since {ref[:10]} on lines the aligner")
        print("otherwise left untouched): new EN sentence copied in, old RU preserved as a `// STALE VERSION:` comment:")
        for f in reworded:
            if f["ru"] == f["new_en"]:
                continue  # was already marked on an earlier run; apply_stale_markers left it alone
            print(f"\n  EN:{f['lineno']} / RU:{f['ru_lineno']}")
            print(f"    {f['new_en']}")
            print(f"    // STALE VERSION: {f['ru']}")

    if ru_existed and ref is None:
        print(f"\nNOTE: no git history found for {ru_path}; skipped the reworded-line check.")

    # Drop blank lines, stale-version marker comments, and lines already
    # explained by another report section before judging a block "real".
    # Content sitting right next to a structural change elsewhere on the
    # page (e.g. a big block removed nearby) can get caught by the same
    # type-mismatch fallback that flags genuine orphans, even though it was
    # already correctly resolved (e.g. as part of a reworded-line mark) --
    # without this, the same line would be reported twice, once correctly
    # and once as a misleading "review this" false alarm.
    already_reported = set()
    for f in reworded:
        already_reported.add(f["new_en"])
        already_reported.add(f["ru"])
        if f["old_en"] is not None:
            already_reported.add(f["old_en"])
    for old, new in replaced:
        already_reported.add(old)
        already_reported.add(new)
    for block in inserted:
        already_reported.update(block)

    real_orphaned = []
    for block in orphaned:
        visible = [
            l for l in block
            if l.strip() and not STALE_MARK_RE.match(l.strip()) and l not in already_reported
        ]
        if visible:
            real_orphaned.append(visible)
    if real_orphaned:
        total = sum(len(b) for b in real_orphaned)
        print(f"\nPOSSIBLY ORPHANED: {total} RU line(s) across {len(real_orphaned)} block(s) have no EN counterpart")
        print("anywhere nearby (left in place, not deleted -- review whether EN removed this on purpose):")
        for block in real_orphaned:
            for l in block:
                print(f"  ? {l}")
            print()

    if inserted or replaced or marked:
        print("\nNext: run scripts/check_pages_translation.sh to locate the newly untranslated lines for translation.")


if __name__ == "__main__":
    main()