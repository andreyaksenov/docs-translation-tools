# Docs consistency scripts

Scripts for checking that the `en/` and `ru/` documentation trees stay in sync.
Run them from the repo root.
On Windows, run them via WSL or Git Bash.

If the scripts lost their executable bit (for example, after downloading or copying the folder), restore it with:

```bash
chmod +x scripts/*.sh
```

Scripts are named `check_<target>_<check>.sh`, where `<target>` is the directory they scan (`pages` covers `pages/` + `partials/`, `examples` covers `examples/`, `images` covers `images/`, `nav` covers `nav.adoc`) and `<check>` is what they verify.

## Examples

### No Cyrillic in examples

```bash
./scripts/check_examples_no_cyrillic.sh
```

Same check as `check_pages_no_cyrillic.sh`, scoped to `en/modules/ROOT/examples` (all file types).

### Orphaned examples

```bash
./scripts/check_examples_orphaned.sh
```

Checks (per language) that every file under `examples/` is pulled in by an `include::example$<path>[]` somewhere in `pages/` or `partials/`.

### Examples parity (EN vs RU)

```bash
./scripts/check_examples_parity.sh
./scripts/check_examples_parity.sh -v   # show a diff for mismatched non-.sql files
```

Checks that `en/modules/ROOT/examples` and `ru/modules/ROOT/examples` have the same files.
Every file must exist on both sides; non-`.sql` files (data/config) must also match byte-for-byte.
`.sql` files only require matching line counts, since their comments are legitimately translated.

## Images

### Orphaned images

```bash
./scripts/check_images_orphaned.sh
```

Checks (per language) that every file under `images/` has its filename referenced somewhere in `pages/` or `partials/`.

## Nav

### Nav structure parity (EN vs RU)

```bash
./scripts/check_nav_structure_parity.sh
./scripts/check_nav_structure_parity.sh -v   # show a diff with file:line references
```

Compares the structural "skeleton" of `nav.adoc` (list depth, `xref:`/`include::` targets) between EN and RU, plus the `nav_reference_*.adoc` partials it includes.
Translated labels are ignored; only the menu structure and link targets are compared.

## Pages

### Broken references

```bash
./scripts/check_pages_broken_refs.sh
```

Checks (per language) that every `xref:`, `include::`, and `injectSvg:`/`injectSvg::` reference found in `pages/`/`partials/` resolves to a real file (page, partial, example, or image).
Cross-component xrefs (`blog::x`, `product-releases:ROOT:x`) are skipped since they point outside this repo; same-page anchor-only xrefs (no `.adoc` target) are skipped too.

### Line parity (EN vs RU)

```bash
./scripts/check_pages_line_parity.sh
```

Checks that every EN `pages/`/`partials/` `.adoc` file has a RU counterpart with the same line count, and vice versa.

### No Cyrillic in pages

```bash
./scripts/check_pages_no_cyrillic.sh
```

Checks that no `pages/`/`partials/` `.adoc` file under `en/modules/ROOT` contains Cyrillic characters — catches RU text accidentally left in (or pasted into) an EN file.

### Orphaned pages (not in nav)

```bash
./scripts/check_pages_orphaned.sh
```

Checks (per language) that every `pages/*.adoc` file is reachable from `nav.adoc`, resolving the `include::partial$...[]` sections nav.adoc pulls in (e.g. SQL command / utility reference lists).
The module's `start_page` (from `antora.yml`) is exempt, since it's not expected to be in the sidebar.

### Structure parity (EN vs RU)

```bash
./scripts/check_pages_structure_parity.sh
./scripts/check_pages_structure_parity.sh -v   # show a diff with file:line references
```

Deeper check for `pages/`/`partials/` `.adoc` files: compares the structural "skeleton" of each EN/RU pair (heading levels, block titles, delimited blocks, block attributes, `include::` directives) so structural drift is caught even when line counts match.

### Untranslated lines

```bash
./scripts/check_pages_translation.sh
./scripts/check_pages_translation.sh -v   # also flag RU lines containing common English stopwords
```

Checks `pages/` and `partials/` `.adoc` files for lines that look like they were never translated: walks EN and RU line-by-line (skipping code blocks, attributes, comments, table cells, and code/keyword-only lines like headings or `term::` definitions) and flags any prose line where RU is byte-identical to EN.

This is a heuristic, not a full AsciiDoc parser — treat findings as a review list, not a hard failure.
