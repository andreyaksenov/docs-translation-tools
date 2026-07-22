# docs_tool.py

A single Python utility for checking that the `en/` and `ru/` documentation
trees stay in sync, and for syncing a RU page's structure after an EN edit.
Run it from the repo root. Requires Python 3.7+ (no third-party dependencies).

Works on both single-module Antora sites (just `en/modules/ROOT`, like this
repo) and multi-module ones (`en/modules/ROOT`, `en/modules/how-to`, ...) --
every module under `en/modules/` and `ru/modules/` is auto-discovered, and
every check scans all of them. Run `scripts/docs_tool.py --list-modules` to
see what was found.

If it lost its executable bit (for example, after downloading or copying the
folder), restore it with:

```bash
chmod +x scripts/docs_tool.py
```

## Usage

```bash
scripts/docs_tool.py --check-<name> [--check-<name> ...] [-v]
scripts/docs_tool.py --all-checks [-v]
scripts/docs_tool.py --sync <path/to/en/file.adoc> [-n] [--since REF]
scripts/docs_tool.py --list-checks
scripts/docs_tool.py --list-modules
```

Multiple `--check-*` flags can be combined in one run. Exits `0` if every
selected check passed, `1` if any check found something.

## Checks

Flags are named `--check-<target>-<check>`, where `<target>` is the directory
scanned (`pages` covers `pages/` + `partials/`, `examples` covers `examples/`,
`images` covers `images/`, `nav` covers `nav.adoc`) and `<check>` is what it
verifies. Run `scripts/docs_tool.py --list-checks` to see the full list.

### Examples

**`--check-examples-no-cyrillic`**
Same check as `--check-pages-no-cyrillic`, scoped to `en/modules/ROOT/examples` (all file types).

**`--check-examples-orphaned`**
Checks (per language) that every file under `examples/` is pulled in by an `include::example$<path>[]` somewhere in `pages/` or `partials/`.

**`--check-examples-parity`** (`-v` shows a diff for mismatched non-`.sql` files)
Checks that `en/modules/ROOT/examples` and `ru/modules/ROOT/examples` have the same files. Every file must exist on both sides; non-`.sql` files (data/config) must also match byte-for-byte. `.sql` files only require matching content once comment-only lines are blanked out, since their comments are legitimately translated.

### Images

**`--check-images-orphaned`**
Checks (per language) that every file under `images/` has its filename referenced somewhere in `pages/` or `partials/`.

### Nav

**`--check-nav-structure-parity`** (`-v` shows a diff with file:line references)
Compares the structural "skeleton" of `nav.adoc` (list depth, `xref:`/`include::` targets) between EN and RU, plus the `nav_reference_*.adoc` partials it includes. Translated labels are ignored; only the menu structure and link targets are compared.

### Pages

**`--check-pages-broken-refs`**
Checks (per language) that every `xref:`, `include::`, and `injectSvg:`/`injectSvg::` reference found in `pages/`/`partials/` resolves to a real file (page, partial, example, or image). Cross-component xrefs (`blog::x`, `product-releases:ROOT:x`) are skipped since they point outside this repo; same-page anchor-only xrefs (no `.adoc` target) are skipped too.

**`--check-pages-line-parity`**
Checks that every EN `pages/`/`partials/` `.adoc` file has a RU counterpart with the same line count, and vice versa.

**`--check-pages-no-cyrillic`**
Checks that no `pages/`/`partials/` `.adoc` file under `en/modules/ROOT` contains Cyrillic characters — catches RU text accidentally left in (or pasted into) an EN file.

**`--check-pages-no-unicode-dashes`**
Checks (per language) that no `pages/`/`partials/` `.adoc` file contains a literal en dash (`–`, U+2013) or em dash (`—`, U+2014) — house style uses `--` (rendered as an em dash by AsciiDoc) instead.

**`--check-pages-orphaned`**
Checks (per language) that every `pages/*.adoc` file is reachable from `nav.adoc`, resolving the `include::partial$...[]` sections nav.adoc pulls in (e.g. SQL command / utility reference lists). The module's `start_page` (from `antora.yml`) is exempt, since it's not expected to be in the sidebar.

**`--check-pages-structure-parity`** (`-v` shows a diff with file:line references)
Deeper check for `pages/`/`partials/` `.adoc` files: compares the structural "skeleton" of each EN/RU pair (heading levels, block titles, delimited blocks, block attributes, `include::` directives) so structural drift is caught even when line counts match.

**`--check-pages-translation`** (`-v` also flags RU lines containing common English stopwords)
Checks `pages/` and `partials/` `.adoc` files for lines that look like they were never translated: walks EN and RU line-by-line (skipping code blocks, attributes, comments, table cells, and code/keyword-only lines like headings or `term::` definitions) and flags any prose line where RU is byte-identical to EN.

This is a heuristic, not a full AsciiDoc parser — treat findings as a review list, not a hard failure.

## Sync a RU page after an EN edit

```bash
scripts/docs_tool.py --sync en/modules/ROOT/pages/reference/utils/analyzedb.adoc
scripts/docs_tool.py --sync <path/to/en/file.adoc> -n   # dry run: print the diff instead of writing
```

Only ever writes the RU counterpart; never touches EN. Aligns RU's structure (headings, anchors, delimited blocks, option/flag terms, code lines) to EN's, and copies in new or changed EN lines verbatim (left untranslated) wherever RU has nothing corresponding yet — run `--check-pages-translation` afterward to find them. Existing RU prose is never rewritten or removed; only technical tokens that must be byte-identical across languages (flag names, code/command lines, include paths, ids, file/directory names) are corrected when they've drifted (e.g. a stale `plpythonu` left behind after EN moved to `plpython3u`).

This is a heuristic aligner, not a semantic merge: when an EN paragraph is reworded (not just extended), the new wording is appended after the existing translation rather than replacing it — review and reconcile those cases by hand.
