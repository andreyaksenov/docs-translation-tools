# docs_tool.py

A single Python utility for checking that the `en/` and `ru/` documentation trees stay in sync, and for syncing a RU page's structure after an EN edit.
Run it from the root of the Antora docs repo you want to check.
Requires Python 3.7+ (no third-party dependencies) — nothing to install.

Works on both single-module Antora sites (just `en/modules/ROOT`) and multi-module ones (`en/modules/ROOT`, `en/modules/how-to`, ...).
Every module under `en/modules/` and `ru/modules/` is auto-discovered, and every check scans all of them automatically.
Run `./docs_tool.py --list-modules` to see what was found.

## Get it

It's a single self-contained file.
Copy it into any Antora docs repo without cloning this repo:

```bash
curl -O https://raw.githubusercontent.com/andreyaksenov/docs-translation-tools/main/scripts/docs_tool.py && chmod +x docs_tool.py
```

Run it with an explicit path (`./docs_tool.py ...` or `python3 docs_tool.py ...`) from the repo root.
A bare `docs_tool.py` won't be found by your shell even after `chmod +x`, since the current directory isn't on `$PATH`.
That's normal shell behavior, not a broken install.

## Usage

```bash
./docs_tool.py --check-<name> [--check-<name> ...] [-v] [--external-root NAME=PATH ...]
./docs_tool.py --all-checks [-v]
./docs_tool.py --sync <path/to/en/file.adoc> [-n] [--since REF]
./docs_tool.py --list-checks
./docs_tool.py --list-modules
```

`<name>` is one of:

```
examples-no-cyrillic
examples-orphaned
examples-parity
images-orphaned
nav-structure-parity
pages-broken-refs
pages-line-parity
pages-no-cyrillic
pages-no-unicode-dashes
pages-orphaned
pages-structure-parity
pages-translation
```

(Or `python3 docs_tool.py ...`, per the note above.)

Multiple `--check-*` flags can be combined in one run.
Exits `0` if every selected check passed, `1` if any check found something.

## Checks

Flags are named `--check-<target>-<check>`, where `<target>` is the directory scanned (`pages` covers `pages/` + `partials/`, `examples` covers `examples/`, `images` covers `images/`, `nav` covers `nav.adoc`) and `<check>` is what it verifies.
Every check below runs across all discovered modules automatically (see `--list-modules`), even though the examples say "EN"/"RU" for brevity.
Run `./docs_tool.py --list-checks` to see the full list.

### Examples

- `--check-examples-no-cyrillic`

  Same check as `--check-pages-no-cyrillic`, scoped to each module's `examples/` (all file types).

- `--check-examples-orphaned`

  Checks (per language, per module) that every file under `examples/` is pulled in by an `include::example$<path>[]` somewhere in `pages/` or `partials/`.

- `--check-examples-parity` (`-v` shows a diff for mismatched non-`.sql` files)

  Checks that each module's EN and RU `examples/` directories have the same files.
  Every file must exist on both sides; non-`.sql` files (data/config) must also match byte-for-byte.
  `.sql` files only require matching content once comment-only lines are blanked out, since their comments are legitimately translated.

### Images

- `--check-images-orphaned`

  Checks (per language) that every file under `images/` has its filename referenced somewhere in `pages/` or `partials/` — anywhere in that language across the whole site, not just its own module, since a page in one module can reference another module's image via a qualified `image::<module>:path[]` macro.
  Ends with a total count and combined file size of the orphaned images found, as a rough gauge of cleanup impact.

### Nav

- `--check-nav-structure-parity` (reports the first differing line by default; `-v` shows the full diff with file:line references)

  Compares the structural "skeleton" of each module's `nav.adoc` (list depth, `xref:`/`include::` targets) between EN and RU, plus any `partial$...adoc` files it includes.
  Translated labels are ignored; only the menu structure and link targets are compared.
  Modules without their own `nav.adoc` are silently skipped.

### Pages

- `--check-pages-broken-refs`

  Checks (per language) that every `xref:`, `include::`, and `injectSvg:`/`injectSvg::` reference found in `pages/`/`partials/` resolves to a real file (page, partial, example, or image) or, for anchor-only/fragment xrefs, a real anchor in the target.

    - Comments (`//` lines and `////` blocks) are skipped.
    - A `{doc-attribute}` used inside a reference target (e.g. `xref:{install-link}[]`) is substituted using that file's own `:name: value` attribute definitions before resolving.
    - A module-prefixed `xref:`/`include::` (e.g. `xref:how-to:page.adoc[]`, `include::how-to:partial$foo.adoc[]`) resolves against that sibling module if the prefix matches a discovered module.
      Otherwise it's treated as pointing outside this repo (e.g. `blog::x`, `include::ADCM:ROOT:partial$x.adoc[]`) and skipped.
    - Anchors are matched against:
        - explicit `[#id]`/`[[id]]` markers (a `[[id]]` is recognized wherever it appears on a line, including inline mid-sentence or mid-list-item, not just on a line of its own), *and*
        - headings' Asciidoctor-autogenerated IDs (tried under a few common `idprefix`/`idseparator` conventions, since the site's actual playbook attributes aren't visible to this tool).
          So `== 6.23.3` satisfies `xref:page.adoc#6-23-3[]` even with no explicit anchor written.
          Underscores in a heading (e.g. `=== gp_segment_configuration`) are kept as literal characters, not stripped as italic markup, and non-Latin headings (e.g. Cyrillic RU ones) are slugified correctly too.
    - Anchor resolution follows module- and component-qualified `include::partial$...`/`include::page$...` chains, not just same-module ones.
    - By default, a reference into a component that isn't part of this repo (e.g. `xref:ADCM:ROOT:page.adoc[]`, pulled in from a separate Antora site like an ADCM docs repo) is left unchecked rather than reported broken, since this tool can't see that component's source.
      If you have that component's repo checked out locally, pass `--external-root NAME=PATH` (repeatable) to resolve against it too, e.g. `--external-root ADCM=../docs-adcm`.

- `--check-pages-line-parity`

  Checks that every EN `pages/`/`partials/` `.adoc` file has a RU counterpart with the same line count, and vice versa.

- `--check-pages-no-cyrillic`

  Checks that no `pages/`/`partials/` `.adoc` file under `en/modules/` contains Cyrillic characters — catches RU text accidentally left in (or pasted into) an EN file.

- `--check-pages-no-unicode-dashes`

  Checks (per language) that no `pages/`/`partials/` `.adoc` file contains a literal en dash (`–`, U+2013) or em dash (`—`, U+2014) — house style uses `--` (rendered as an em dash by AsciiDoc) instead.

- `--check-pages-orphaned`

  Checks (per language) that every `pages/*.adoc` file is reachable from some module's `nav.adoc`, resolving the `include::partial$...[]` sections nav.adoc pulls in (e.g. SQL command / utility reference lists) and allowing cross-module nav links.
  The site's `start_page` (from `antora.yml`) is exempt, since it's not expected to be in the sidebar.

- `--check-pages-structure-parity` (reports the first differing line by default; `-v` shows the full diff with file:line references)

  Deeper check for `pages/`/`partials/` `.adoc` files: compares the structural "skeleton" of each EN/RU pair (heading levels, block titles, delimited blocks, block attributes, `include::` directives) so structural drift is caught even when line counts match.

- `--check-pages-translation` (`-v` also flags RU lines containing common English stopwords)

  Checks `pages/` and `partials/` `.adoc` files for lines that look like they were never translated: walks EN and RU line-by-line and flags any prose line where RU is byte-identical to EN, skipping:

    - code blocks, attributes, and comments;
    - table cells;
    - code/keyword-only lines, such as headings and `term::` definitions;
    - a list item that's entirely a `` `code span` `` or a `*_bold-italic UI element name_*`.

This is a heuristic, not a full AsciiDoc parser.
Treat findings as a review list, not a hard failure.

## Sync a RU page after an EN edit

```bash
./docs_tool.py --sync en/modules/ROOT/pages/reference/utils/analyzedb.adoc
./docs_tool.py --sync <path/to/en/file.adoc> -n   # dry run: print the diff instead of writing
```

Only ever writes the RU counterpart; never touches EN.

- Aligns RU's structure to EN's: headings, anchors, delimited blocks, option/flag terms, code lines.
- Copies in new or changed EN lines verbatim (left untranslated) wherever RU has nothing corresponding yet.
  Run `--check-pages-translation` afterward to find them.
- Existing RU prose is never rewritten or removed.
- Only technical tokens that must be byte-identical across languages are corrected when they've drifted (e.g. a stale `plpythonu` left behind after EN moved to `plpython3u`): flag names, code/command lines, include paths, ids, file/directory names.

This is a heuristic aligner, not a semantic merge: when an EN paragraph is reworded (not just extended), the new wording is appended after the existing translation rather than replacing it.
Review and reconcile those cases by hand.
