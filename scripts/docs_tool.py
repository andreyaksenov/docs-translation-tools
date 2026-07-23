#!/usr/bin/env python3
"""
docs_tool.py -- unified consistency-check and EN->RU sync utility for
Antora documentation trees laid out as en/modules/<module>/... and
ru/modules/<module>/... (single-module sites with just a ROOT module work
the same way -- there's simply one module to discover).

This replaces the standalone scripts/check_*.sh scripts and
scripts/sync_pages_from_en.py with a single shareable tool.

Usage:
    ./docs_tool.py --check-<name> [--check-<name> ...] [-v]
    ./docs_tool.py --all-checks [-v]
    ./docs_tool.py --sync <path/to/en/file.adoc> [-n] [--since REF]
    ./docs_tool.py --list-checks
    ./docs_tool.py --list-modules

Run from the repo root (use "python docs_tool.py ..." if it isn't marked
executable, e.g. on Windows). Every check scans all discovered modules
(every directory under en/modules/ and ru/modules/) automatically -- no
flag needed. Examples:

    ./docs_tool.py --check-pages-no-cyrillic
    ./docs_tool.py --check-pages-broken-refs --check-pages-orphaned
    ./docs_tool.py --all-checks -v
    ./docs_tool.py --sync en/modules/ROOT/pages/reference/utils/analyzedb.adoc -n
    ./docs_tool.py --sync en/modules/how-to/pages/manage-cluster/pam.adoc -n

--sync and the checks --list-checks marks (beta) rely on heuristics rather
than a real AsciiDoc parser and can misfire on legitimate content -- treat
their output as a review list, not a hard gate.
"""
import argparse
import difflib
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

EN_MODULES_ROOT = Path("en/modules")
RU_MODULES_ROOT = Path("ru/modules")

CYRILLIC_RE = re.compile(r'[Ѐ-ӿ]')
EN_EM_DASH_RE = re.compile(r'[–—]')

# Zero-width/invisible-by-definition Unicode ranges: ZWSP/ZWNJ/ZWJ/bidi marks,
# soft hyphen, the Mongolian vowel separator, bidi embedding/override/isolate
# controls, word joiner and the invisible math operators, the BOM, and the
# Unicode tag characters (U+E0000-U+E007F) -- a range with no visible glyph
# at all, known to be abused to smuggle hidden ASCII text past a casual read
# of the source (an "ASCII smuggling" trick), not just a typography quirk.
_INVISIBLE_RANGES = (
    (0x00AD, 0x00AD),
    (0x180E, 0x180E),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x2064),
    (0x2066, 0x2069),
    (0xFEFF, 0xFEFF),
    (0xE0000, 0xE007F),
)
_INVISIBLE_RE = re.compile('[' + ''.join(
    re.escape(chr(lo)) if lo == hi else f"{re.escape(chr(lo))}-{re.escape(chr(hi))}"
    for lo, hi in _INVISIBLE_RANGES
) + ']')

# Populated from --external-root NAME=PATH (see main()). Lets
# --check-pages-broken-refs resolve xref:/include:: targets that point at a
# separate Antora component (e.g. `xref:ADCM:ROOT:page.adoc[]`) when the
# user has that component's repo checked out locally -- otherwise such
# targets are silently treated as pointing outside anything this tool can
# see, and left unchecked. {component_name: {"en": {module: root}, "ru": {module: root}}}
EXTERNAL_COMPONENTS = {}


# --------------------------------------------------------------------------
# Module discovery
# --------------------------------------------------------------------------

def discover_module_names():
    """Every module directory found under en/modules/ and/or ru/modules/,
    sorted for stable output. A single-module site (just en/modules/ROOT)
    yields ["ROOT"]; a multi-module Antora site yields every module
    (ROOT, concept, how-to, ...) whether or not it has a RU counterpart yet
    (a missing RU module still produces useful MISSING findings)."""
    names = set()
    for base in (EN_MODULES_ROOT, RU_MODULES_ROOT):
        if base.is_dir():
            names.update(p.name for p in base.iterdir() if p.is_dir())
    return sorted(names)


def module_roots():
    """Yield (module_name, en_root, ru_root) for every discovered module."""
    for name in discover_module_names():
        yield name, EN_MODULES_ROOT / name, RU_MODULES_ROOT / name


def _load_external_components(specs):
    """Parse --external-root NAME=PATH values into
    {component_name: {"en": {module: root}, "ru": {module: root}}}, by
    running the same en/modules + ru/modules discovery this tool uses on
    its own repo against each external repo root."""
    components = {}
    for spec in specs or []:
        if "=" not in spec:
            sys.exit(f"error: --external-root must be NAME=PATH, got: {spec!r}")
        name, _, path_str = spec.partition("=")
        repo_root = Path(path_str)
        en_root = repo_root / "en" / "modules"
        ru_root = repo_root / "ru" / "modules"
        module_names = set()
        for base in (en_root, ru_root):
            if base.is_dir():
                module_names.update(p.name for p in base.iterdir() if p.is_dir())
        components[name] = {
            "en": {m: en_root / m for m in module_names},
            "ru": {m: ru_root / m for m in module_names},
        }
    return components


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

def _read_lines(path: Path):
    """Read a text file as a list of lines (no trailing newlines), tolerating
    encoding issues the way the shell tools (grep/perl -CSD) silently did."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, UnicodeError):
        return None


def _read_text(path: Path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _iter_files(root: Path, suffix: str = None):
    """Yield all files under root (recursively), optionally filtered by
    suffix (e.g. '.adoc'). Sorted for stable, reproducible output."""
    if not root.is_dir():
        return
    for p in sorted(root.rglob("*")):
        if p.is_file() and (suffix is None or p.suffix == suffix):
            yield p


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def _labeled_unified_diff(en_lines, ru_lines, en_label, ru_label, n=1):
    """Render a unified diff with each changed line prefixed by its source
    file label, matching the `sed -e "s|^-|- $en_file:  |" ...` treatment
    used throughout the original shell scripts so findings are easy to jump
    to directly from the terminal."""
    diff = difflib.unified_diff(en_lines, ru_lines, lineterm="", n=n)
    out = []
    for line in diff:
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("-"):
            out.append(f"    - {en_label}:  {line[1:]}")
        elif line.startswith("+"):
            out.append(f"    + {ru_label}:  {line[1:]}")
        else:
            out.append(f"          {line[1:] if line.startswith(' ') else line}")
    return out


# --------------------------------------------------------------------------
# EXAMPLES checks
# --------------------------------------------------------------------------

def check_examples_no_cyrillic(verbose=False) -> bool:
    """Port of check_examples_no_cyrillic.sh: no Cyrillic in en/ examples
    (checked across every module)."""
    ok = True
    for _, en_root, _ in module_roots():
        for f in _iter_files(en_root / "examples"):
            lines = _read_lines(f)
            if lines is None:
                continue
            hits = [(i, l) for i, l in enumerate(lines, 1) if CYRILLIC_RE.search(l)]
            if hits:
                ok = False
                print(f"FILE     {f}")
                for i, l in hits:
                    print(f"  line {i}: {l}")
    if ok:
        print("OK: no Cyrillic characters found in en/ examples.")
    return ok


def check_examples_orphaned(verbose=False) -> bool:
    """Port of check_examples_orphaned.sh: every examples/ file must be
    pulled in by an include::example$<path>[] somewhere in that module's
    pages/partials."""
    ok = True
    for _, en_root, ru_root in module_roots():
        for root in (en_root, ru_root):
            examples_root = root / "examples"
            if not examples_root.is_dir():
                continue
            corpus_parts = []
            for d in (root / "pages", root / "partials"):
                for f in _iter_files(d):
                    text = _read_text(f)
                    if text is not None:
                        corpus_parts.append(text)
            corpus = "\n".join(corpus_parts)
            for f in _iter_files(examples_root):
                rel = f.relative_to(examples_root).as_posix()
                needle = f"example$${rel}".replace("$$", "$")
                if needle not in corpus:
                    ok = False
                    print(f"ORPHANED  {f}  (not included in any pages/partials)")
    if ok:
        print("OK: all examples are included somewhere.")
    return ok


_COMMENT_STRIP_RE = re.compile(r'[ \t]+--([ \t].*)?$')


def _blank_sql_comments(text: str):
    """Port of the awk `blank_comments` helper: blanks out comment-only
    lines (-- line comments and /* ... */ blocks) so translated SQL
    comments don't count as content drift, while still comparing actual
    code lines (including indentation) verbatim."""
    out = []
    in_comment = False
    for line in text.splitlines():
        trimmed = line.strip()
        if in_comment:
            out.append("")
            if "*/" in trimmed:
                in_comment = False
            continue
        if trimmed == "" or trimmed.startswith("--"):
            out.append("")
            continue
        if trimmed.startswith("/*"):
            out.append("")
            if "*/" not in trimmed:
                in_comment = True
            continue
        out.append(_COMMENT_STRIP_RE.sub("", line))
    return out


def check_examples_parity(verbose=False) -> bool:
    """Port of check_examples_parity.sh: en/ru examples must have the same
    files (per module); non-.sql files must match byte-for-byte, .sql files
    only need to match once comment-only lines are blanked out."""
    ok = True

    def skip(f: Path, base: Path):
        return "_demo_cluster" in f.relative_to(base).parts

    for _, en_root, ru_root in module_roots():
        en_examples = en_root / "examples"
        ru_examples = ru_root / "examples"

        for en_file in _iter_files(en_examples):
            if skip(en_file, en_examples):
                continue
            rel = en_file.relative_to(en_examples)
            ru_file = ru_examples / rel
            if not ru_file.is_file():
                print(f"MISSING  {en_file}  (no ru counterpart)")
                ok = False
                continue

            if en_file.suffix == ".sql":
                en_text = _read_text(en_file) or ""
                ru_text = _read_text(ru_file) or ""
                en_blanked = _blank_sql_comments(en_text)
                ru_blanked = _blank_sql_comments(ru_text)
                if en_blanked != ru_blanked:
                    print(f"DIFF     {en_file}")
                    print(f"         {ru_file}")
                    ok = False
                    if verbose:
                        print("\n".join(_labeled_unified_diff(en_blanked, ru_blanked, en_file, ru_file)))
                        print()
                continue

            en_bytes = en_file.read_bytes()
            ru_bytes = ru_file.read_bytes()
            if en_bytes != ru_bytes:
                print(f"DIFF     {en_file}")
                print(f"         {ru_file}")
                ok = False
                if verbose:
                    en_lines = (_read_text(en_file) or "").splitlines()
                    ru_lines = (_read_text(ru_file) or "").splitlines()
                    print("\n".join(_labeled_unified_diff(en_lines, ru_lines, en_file, ru_file)))
                    print()

        for ru_file in _iter_files(ru_examples):
            if skip(ru_file, ru_examples):
                continue
            rel = ru_file.relative_to(ru_examples)
            if not (en_examples / rel).is_file():
                print(f"MISSING  {ru_file}  (no en counterpart)")
                ok = False

    if ok:
        print("OK: en/ru examples match.")
    return ok


# --------------------------------------------------------------------------
# IMAGES checks
# --------------------------------------------------------------------------

def check_images_orphaned(verbose=False) -> bool:
    """Port of check_images_orphaned.sh: every images/ file's basename must
    be referenced somewhere in pages/partials -- anywhere in the site for
    that language, not just its own module, since a page in one module can
    reference another module's image via a qualified
    image::<module>:path[] macro (the basename still appears as a
    substring of that qualified target, so no path-aware matching is
    needed once the corpus covers the whole site)."""
    ok = True
    orphaned_bytes = 0
    orphaned_count = 0
    modules = list(module_roots())
    for lang_roots in (
            [en_root for _, en_root, _ in modules],
            [ru_root for _, _, ru_root in modules],
    ):
        corpus_parts = []
        for root in lang_roots:
            for d in (root / "pages", root / "partials"):
                for f in _iter_files(d):
                    text = _read_text(f)
                    if text is not None:
                        corpus_parts.append(text)
        corpus = "\n".join(corpus_parts)
        for root in lang_roots:
            images_root = root / "images"
            if not images_root.is_dir():
                continue
            for f in _iter_files(images_root):
                if f.name not in corpus:
                    ok = False
                    orphaned_count += 1
                    orphaned_bytes += f.stat().st_size
                    print(f"ORPHANED  {f}  (not referenced in any pages/partials)")
    if ok:
        print("OK: all images are referenced somewhere.")
    else:
        print(f"\nTotal: {orphaned_count} orphaned image(s), {_format_size(orphaned_bytes)}")
    return ok


# --------------------------------------------------------------------------
# NAV checks
# --------------------------------------------------------------------------

_NAV_SVG_RE = re.compile(r'^(\*+)\s\+\+\+<svg><use xlink:href="[^"]*#([^"]+)"')
_NAV_XREF_RE = re.compile(r'^(\*+)\s+xref:([^\[]+)\[')
_NAV_LISTITEM_RE = re.compile(r'^(\*+)\s')
_NAV_INCLUDE_RE = re.compile(r'^include::(.*)$')
_INCLUDE_PARTIAL_RE = re.compile(r'include::partial\$([^\[]+\.adoc)')


def _nav_skeleton(path: Path):
    """Structural skeleton of a nav file: list depth + xref/include target,
    or an <svg:...>/<text> placeholder. Numbered lines (1-based) for -v
    lookup; caller strips the number prefix for the plain equality check."""
    lines = _read_lines(path)
    if lines is None:
        return []
    out = []
    for lineno, line in enumerate(lines, 1):
        m = _NAV_SVG_RE.match(line)
        if m:
            out.append((lineno, f"{m.group(1)} <svg:{m.group(2)}>"))
            continue
        m = _NAV_XREF_RE.match(line)
        if m:
            out.append((lineno, f"{m.group(1)} xref:{m.group(2)}"))
            continue
        m = _NAV_LISTITEM_RE.match(line)
        if m:
            out.append((lineno, f"{m.group(1)} <text>"))
            continue
        m = _NAV_INCLUDE_RE.match(line)
        if m:
            out.append((lineno, f"include::{m.group(1)}"))
    return out


def _skeleton_diff_lines(en_skel, ru_skel, en_label, ru_label):
    """Like _labeled_unified_diff, but diffs the skeleton *content* only
    (ignoring line numbers) so that lines shifting by a line or two --
    normal given EN/RU text length differences -- don't make every
    subsequent equal entry look like a spurious diff. Line numbers are
    still shown, just not used to decide what counts as a difference."""
    en_plain = [s for _, s in en_skel]
    ru_plain = [s for _, s in ru_skel]
    sm = difflib.SequenceMatcher(a=en_plain, b=ru_plain, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        for i in range(i1, i2):
            out.append(f"    - {en_label}:  {en_skel[i][0]}:{en_skel[i][1]}")
        for j in range(j1, j2):
            out.append(f"    + {ru_label}:  {ru_skel[j][0]}:{ru_skel[j][1]}")
    return out


def _compare_skeleton_pair(en_file: Path, ru_file: Path, skeleton_fn, verbose) -> bool:
    en_skel = skeleton_fn(en_file)
    ru_skel = skeleton_fn(ru_file)
    en_plain = [s for _, s in en_skel]
    ru_plain = [s for _, s in ru_skel]
    if en_plain == ru_plain:
        return True
    print(f"DIFF     {en_file}")
    print(f"         {ru_file}")
    if verbose:
        print("\n".join(_skeleton_diff_lines(en_skel, ru_skel, en_file, ru_file)))
        print()
    else:
        i = next((i for i in range(min(len(en_plain), len(ru_plain))) if en_plain[i] != ru_plain[i]),
                 min(len(en_plain), len(ru_plain)))
        en_lineno = en_skel[i][0] if i < len(en_skel) else "EOF"
        ru_lineno = ru_skel[i][0] if i < len(ru_skel) else "EOF"
        print(f"         first difference: {en_file}:{en_lineno}  vs  {ru_file}:{ru_lineno}  (rerun with -v for the full diff)")
    return False


def check_nav_structure_parity(verbose=False) -> bool:
    """Port of check_nav_structure_parity.sh. A module only has a nav.adoc
    of its own on some multi-module Antora sites (e.g. a top-level ROOT nav
    plus a second one for a "how-to" module); modules without one are
    silently skipped."""
    ok = True
    any_nav = False
    for _, en_root, ru_root in module_roots():
        en_nav = en_root / "nav.adoc"
        ru_nav = ru_root / "nav.adoc"
        if not (en_nav.is_file() and ru_nav.is_file()):
            continue
        any_nav = True
        if not _compare_skeleton_pair(en_nav, ru_nav, _nav_skeleton, verbose):
            ok = False

        en_text = _read_text(en_nav) or ""
        for partial_name in _INCLUDE_PARTIAL_RE.findall(en_text):
            en_partial = en_root / "partials" / partial_name
            ru_partial = ru_root / "partials" / partial_name
            if en_partial.is_file() and ru_partial.is_file():
                if not _compare_skeleton_pair(en_partial, ru_partial, _nav_skeleton, verbose):
                    ok = False

    if not any_nav:
        print("OK: no nav.adoc found to compare.")
    elif ok:
        print("OK: nav structure matches for en/ru.")
    return ok


# --------------------------------------------------------------------------
# PAGES: broken references
# --------------------------------------------------------------------------

_REF_SCAN_RE = re.compile(r'(?:xref:|include::|injectSvg:{1,2})[^\]\[\s]+\[')
_ANCHOR_ID_TPL = r'^\[#{0}\]$|\[\[{0}(,|\]\])'
_INCLUDE_CONTENT_RE = re.compile(
    r'include::(?:([A-Za-z][A-Za-z0-9_-]*):)?(?:([A-Za-z][A-Za-z0-9_-]*):)?(partial|page)\$([^\[]+\.adoc)'
)
_COMPONENT_PREFIX_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]*:')


def _strip_root_slash(t: str) -> str:
    """Antora resource-id-style targets (xref page paths, family$ include
    paths, injectSvg targets) are always relative to their module/family
    root -- a leading '/' some authors add for emphasis isn't filesystem
    syntax there. Left unstripped, `root / "pages" / t` would silently
    treat it as an absolute path and check against the OS filesystem root
    instead of the intended file (see the toast-storage.adoc glossary
    xref that surfaced this)."""
    return t.lstrip("/")

_HEADING_ID_RE = re.compile(r'^=+\s+(.*\S)\s*$')
_ID_STRIP_MARKUP_RE = re.compile(r'[`*]')
_ID_INVALID_CHARS_RE = re.compile(r'[^\w]+', re.UNICODE)
_ID_PREFIX_SEP_COMBOS = (("", "-"), ("_", "_"), ("", "_"), ("_", "-"))


def _heading_autoids(title: str):
    """Anchors are usually left implicit: a heading like `== 6.23.3` gets an
    ID Asciidoctor derives from its text, not one written in the source, so
    `_anchor_exists` can't just grep for `[#id]`/`[[id]]` -- it has to
    reproduce that derivation. The exact result depends on the site's
    `idprefix`/`idseparator` attributes (not visible to this tool, since
    they live in the Antora playbook, not this repo), so this tries a few
    common conventions and accepts a match against any of them."""
    plain = _ID_STRIP_MARKUP_RE.sub("", title).lower()
    return {
        prefix + _ID_INVALID_CHARS_RE.sub(sep, plain).strip(sep)
        for prefix, sep in _ID_PREFIX_SEP_COMBOS
    }


def _resolve_module_ref(name, rest, lang_module_roots, lang):
    """Resolve a single `name:` prefix already peeled off a target/include
    path. `name` may be a module in this repo's current language, or (if
    registered via --external-root) a sibling Antora component -- in which
    case an optional following `module:` segment at the start of `rest`
    selects the module within it (defaulting to ROOT, same as Antora).
    Returns (target_root, remaining_rest), or None if `name` names
    something this tool can't resolve (unregistered external component --
    left unchecked, not reported broken)."""
    if name in lang_module_roots:
        return lang_module_roots[name], rest
    modules = EXTERNAL_COMPONENTS.get(name, {}).get(lang)
    if modules is None:
        return None
    m = _COMPONENT_PREFIX_RE.match(rest)
    if m:
        module = m.group(0)[:-1]
        if module in modules:
            return modules[module], rest[len(m.group(0)):]
        return None
    if "ROOT" in modules:
        return modules["ROOT"], rest
    return None


def _collect_include_partials(file: Path, root: Path, lang_module_roots=None, lang="en", depth=0, seen=None):
    """A page's anchors may live in content it pulls in via
    include::partial$...[] or include::page$...[] (recursively, and
    possibly module- or component-qualified, e.g.
    include::how-to:partial$...[] or include::ADCM:ROOT:partial$...[]), not
    its own source. Depth-capped to guard against an accidental include
    cycle."""
    if seen is None:
        seen = set()
    if depth > 5 or file in seen:
        return []
    seen.add(file)
    result = [file]
    text = _read_text(file)
    if text is None:
        return result
    lang_module_roots = lang_module_roots or {}
    for prefix1, prefix2, family, name in _INCLUDE_CONTENT_RE.findall(text):
        target_root = root
        if prefix1:
            resolved = _resolve_module_ref(prefix1, f"{prefix2}:" if prefix2 else "", lang_module_roots, lang)
            if resolved is None:
                continue  # external component's content, not registered via --external-root
            target_root, _ = resolved
        subdir = "partials" if family == "partial" else "pages"
        target_file = target_root / subdir / _strip_root_slash(name)
        if target_file.is_file():
            result.extend(_collect_include_partials(target_file, target_root, lang_module_roots, lang, depth + 1, seen))
    return result


def _anchor_exists(target_file: Path, anchor_id: str, root: Path, lang_module_roots=None, lang="en") -> bool:
    pattern = re.compile(_ANCHOR_ID_TPL.format(re.escape(anchor_id)))
    for f in _collect_include_partials(target_file, root, lang_module_roots, lang):
        text = _read_text(f)
        if text is None:
            continue
        for line in text.splitlines():
            if pattern.search(line):
                return True
            m = _HEADING_ID_RE.match(line)
            if m and anchor_id in _heading_autoids(m.group(1)):
                return True
    return False


def _excluded_ref_lines(path: Path) -> set:
    """Line numbers to skip when scanning for references: AsciiDoc line
    (`//`) and block (`////`) comments, and anything inside a ---- / ....
    literal/listing block."""
    lines = _read_lines(path)
    if lines is None:
        return set()
    excluded = set()
    in_code = False
    in_comment = False
    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if re.match(r'^/{4,}\s*$', line):
            in_comment = not in_comment
            excluded.add(lineno)
            continue
        if in_comment:
            excluded.add(lineno)
            continue
        if stripped.startswith("//"):
            excluded.add(lineno)
            continue
        if re.match(r'^(----|\.\.\.\.)\s*$', line):
            in_code = not in_code
            excluded.add(lineno)
            continue
        if in_code:
            excluded.add(lineno)
    return excluded


_DOC_ATTR_DEF_RE = re.compile(r'^:([A-Za-z0-9_-]+):\s*(.*)$')
_ATTR_REF_RE = re.compile(r'\{([A-Za-z0-9_-]+)\}')


def _collect_doc_attrs(lines):
    """Document attributes (`:name: value`) defined anywhere in the file, so
    a reference target using `{name}` (e.g. `xref:{install-link}[]`) can be
    resolved the way Asciidoctor would substitute it. Attributes are only
    collected from the file itself, not from included partials or the
    Antora playbook, so a target relying on those is left unresolved (and
    reported, same as an unknown attribute)."""
    attrs = {}
    for line in lines:
        m = _DOC_ATTR_DEF_RE.match(line)
        if m:
            attrs[m.group(1)] = m.group(2).strip()
    return attrs


def _substitute_attrs(text, attrs):
    return _ATTR_REF_RE.sub(lambda m: attrs.get(m.group(1), m.group(0)), text)


def _check_refs_in_file(file: Path, root: Path, report, lang_module_roots=None, lang="en", partial_includers=None):
    """`lang_module_roots` (name -> module root, for the same language as
    `root`) lets a component-prefixed xref (`xref:other-module:page.adoc[]`)
    resolve against a sibling module on multi-module Antora sites, instead
    of always being treated as pointing outside this repo. `lang` selects
    which side of any registered --external-root component to resolve
    against. `partial_includers` (see _build_partial_includers) lets an
    *unqualified* xref/include written inside a partial be checked against
    the module(s) that actually include it -- that's the context Antora
    resolves it in, not the partial file's own directory."""
    lines = _read_lines(file)
    if lines is None:
        return
    excluded = _excluded_ref_lines(file)
    directory = file.parent
    lang_module_roots = lang_module_roots or {}
    doc_attrs = _collect_doc_attrs(lines)
    fallback_roots = (partial_includers or {}).get(file) or {root}

    for lineno, line in enumerate(lines, 1):
        if lineno in excluded:
            continue
        for m in _REF_SCAN_RE.finditer(line):
            target = m.group(0)[:-1]  # strip trailing '['
            if "{" in target:
                target = _substitute_attrs(target, doc_attrs)

            if target.startswith("xref:"):
                t = target[len("xref:"):]
                candidate_roots = list(fallback_roots)
                m_component = _COMPONENT_PREFIX_RE.match(t)
                if m_component:
                    component = m_component.group(0)[:-1]  # strip trailing ':'
                    resolved = _resolve_module_ref(component, t[len(m_component.group(0)):], lang_module_roots, lang)
                    if resolved is None:
                        continue  # external component xref (blog::x, ...)
                    candidate_roots = [resolved[0]]
                    t = resolved[1]

                fragment = ""
                if "#" in t:
                    t, fragment = t.split("#", 1)

                if t.endswith(".adoc"):
                    page_t = _strip_root_slash(t)
                    found_root = next((cand for cand in candidate_roots if (cand / "pages" / page_t).is_file()), None)
                    if found_root is None:
                        report(file, lineno, f"xref:{t}")
                    elif fragment and not _anchor_exists(found_root / "pages" / page_t, fragment, found_root, lang_module_roots, lang):
                        report(file, lineno, f"xref:{t}#{fragment} (anchor not found)")
                elif t:
                    if not any(_anchor_exists(file, t, cand, lang_module_roots, lang) for cand in candidate_roots):
                        report(file, lineno, f"xref:{t} (anchor not found)")

            elif target.startswith("include::"):
                t = target[len("include::"):]
                candidate_roots = list(fallback_roots)
                m_component = _COMPONENT_PREFIX_RE.match(t)
                if m_component:
                    component = m_component.group(0)[:-1]  # strip trailing ':'
                    resolved = _resolve_module_ref(component, t[len(m_component.group(0)):], lang_module_roots, lang)
                    if resolved is None:
                        continue  # external component/module include (ADCM:ROOT:..., ...)
                    candidate_roots = [resolved[0]]
                    t = resolved[1]

                if t.startswith("partial$"):
                    name = _strip_root_slash(t[len("partial$"):])
                    if not any((cand / "partials" / name).is_file() for cand in candidate_roots):
                        report(file, lineno, target)
                elif t.startswith("example$"):
                    name = _strip_root_slash(t[len("example$"):])
                    if not any((cand / "examples" / name).is_file() for cand in candidate_roots):
                        report(file, lineno, target)
                elif t.startswith("page$"):
                    name = _strip_root_slash(t[len("page$"):])
                    if not any((cand / "pages" / name).is_file() for cand in candidate_roots):
                        report(file, lineno, target)
                elif not (directory / t).is_file():
                    report(file, lineno, target)

            elif target.startswith("injectSvg::"):
                t = _strip_root_slash(target[len("injectSvg::"):])
                if not (root / "images" / t).is_file():
                    report(file, lineno, f"injectSvg::{t}")

            elif target.startswith("injectSvg:"):
                t = _strip_root_slash(target[len("injectSvg:"):])
                if not (root / "images" / t).is_file():
                    report(file, lineno, f"injectSvg:{t}")


def _build_partial_includers(module_list, lang_module_roots, lang):
    """{partial_file: {module_root, ...}} for every partials/*.adoc file
    (in this language) actually pulled in via include::...partial$...[]
    somewhere in the site. Antora resolves an unqualified xref/include
    found *inside* a partial using the context of whichever page includes
    it -- the partial's content becomes part of that page's document during
    conversion -- not the partial file's own directory. So a bare
    `xref:foo.adoc[]` written in a partial that's only ever included from
    module A must be checked against module A, even though the partial
    physically lives under module B's partials/."""
    includers = {}
    for _, module_root in module_list:
        for f in list(_iter_files(module_root / "pages", ".adoc")) + list(_iter_files(module_root / "partials", ".adoc")):
            text = _read_text(f)
            if text is None:
                continue
            for prefix1, prefix2, family, name in _INCLUDE_CONTENT_RE.findall(text):
                if family != "partial":
                    continue
                target_root = module_root
                if prefix1:
                    resolved = _resolve_module_ref(prefix1, f"{prefix2}:" if prefix2 else "", lang_module_roots, lang)
                    if resolved is None:
                        continue
                    target_root, _ = resolved
                includers.setdefault(target_root / "partials" / _strip_root_slash(name), set()).add(module_root)
    return includers


def check_pages_broken_refs(verbose=False) -> bool:
    """Port of check_pages_broken_refs.sh, extended to resolve
    component-prefixed xrefs against sibling modules of the same language
    when the component name matches a discovered module."""
    ok = True

    def report(file, lineno, msg):
        nonlocal ok
        ok = False
        print(f"BROKEN   {file}:{lineno}  {msg}")

    modules = list(module_roots())
    en_module_roots = {name: en_root for name, en_root, _ in modules}
    ru_module_roots = {name: ru_root for name, _, ru_root in modules}
    en_module_list = [(name, en_root) for name, en_root, _ in modules]
    ru_module_list = [(name, ru_root) for name, _, ru_root in modules]
    en_includers = _build_partial_includers(en_module_list, en_module_roots, "en")
    ru_includers = _build_partial_includers(ru_module_list, ru_module_roots, "ru")

    for _, en_root, ru_root in modules:
        for lang, root, lang_module_roots, includers in (
                ("en", en_root, en_module_roots, en_includers),
                ("ru", ru_root, ru_module_roots, ru_includers),
        ):
            for f in list(_iter_files(root / "pages", ".adoc")) + list(_iter_files(root / "partials", ".adoc")):
                _check_refs_in_file(f, root, report, lang_module_roots, lang, includers)

    if ok:
        print("OK: no broken xref/include/image references found.")
    return ok


# --------------------------------------------------------------------------
# PAGES: line parity
# --------------------------------------------------------------------------

def check_pages_line_parity(verbose=False) -> bool:
    """Port of check_pages_line_parity.sh (matches `wc -l` semantics: counts
    newline characters, not logical/visual lines)."""
    ok = True
    for _, en_root, ru_root in module_roots():
        for subdir in ("pages", "partials"):
            for en_file in _iter_files(en_root / subdir, ".adoc"):
                rel = en_file.relative_to(en_root)
                ru_file = ru_root / rel
                if not ru_file.is_file():
                    print(f"MISSING  {en_file}  (no ru counterpart)")
                    ok = False
                    continue
                en_n = (_read_text(en_file) or "").count("\n")
                ru_n = (_read_text(ru_file) or "").count("\n")
                if en_n != ru_n:
                    print(f"DIFF     {en_file}  ({en_n} lines)")
                    print(f"         {ru_file}  ({ru_n} lines)")
                    ok = False

            for ru_file in _iter_files(ru_root / subdir, ".adoc"):
                rel = ru_file.relative_to(ru_root)
                if not (en_root / rel).is_file():
                    print(f"MISSING  {ru_file}  (no en counterpart)")
                    ok = False

    if ok:
        print("OK: all compared en/ru pages have matching line counts.")
    return ok


# --------------------------------------------------------------------------
# PAGES: no Cyrillic / no unicode dashes
# --------------------------------------------------------------------------

def check_pages_no_cyrillic(verbose=False) -> bool:
    """Port of check_pages_no_cyrillic.sh (en/ only, all modules)."""
    ok = True
    for _, en_root, _ in module_roots():
        for f in list(_iter_files(en_root / "pages", ".adoc")) + list(_iter_files(en_root / "partials", ".adoc")):
            lines = _read_lines(f)
            if lines is None:
                continue
            hits = [(i, l) for i, l in enumerate(lines, 1) if CYRILLIC_RE.search(l)]
            if hits:
                ok = False
                print(f"FILE     {f}")
                for i, l in hits:
                    print(f"  line {i}: {l}")
    if ok:
        print("OK: no Cyrillic characters found in en/ pages.")
    return ok


def _invisible_char_label(ch: str) -> str:
    """Human-readable label for an invisible character: its Unicode name if
    it has one, alongside the codepoint -- some tag characters format as
    nothing printable of their own, so the codepoint is sometimes all
    there is to go on."""
    try:
        name = unicodedata.name(ch)
    except ValueError:
        name = "UNKNOWN"
    return f"U+{ord(ch):04X} {name}"


def _mark_invisible_chars(line: str) -> str:
    """Render a line with every invisible character swapped for a visible
    marker -- printed verbatim, a hit would be indistinguishable from a
    clean line, which would defeat the point of the check."""
    return _INVISIBLE_RE.sub(lambda m: f"⟦U+{ord(m.group(0)):04X}⟧", line)


def check_pages_no_invisible_chars(verbose=False) -> bool:
    """New check (not a port of an existing shell script): flags zero-width
    and other invisible/formatting Unicode characters -- ZWSP, ZWNJ, ZWJ,
    word joiner, BOM, bidi control marks, and Unicode tag characters -- in
    en/ru pages/partials (see _INVISIBLE_RANGES for the full list and why).
    These render as nothing, so unlike the Cyrillic/dash checks, hits are
    reported with the character swapped for a visible marker rather than
    printed as-is."""
    ok = True
    total_hits = 0
    for _, en_root, ru_root in module_roots():
        for root in (en_root, ru_root):
            for f in list(_iter_files(root / "pages", ".adoc")) + list(_iter_files(root / "partials", ".adoc")):
                lines = _read_lines(f)
                if lines is None:
                    continue
                hits = [(i, l) for i, l in enumerate(lines, 1) if _INVISIBLE_RE.search(l)]
                if hits:
                    ok = False
                    total_hits += len(hits)
                    print(f"FILE     {f}")
                    for i, l in hits:
                        labels = ", ".join(sorted({_invisible_char_label(ch) for ch in _INVISIBLE_RE.findall(l)}))
                        print(f"  line {i}: {labels}")
                        if verbose:
                            print(f"    {_mark_invisible_chars(l)}")
    if ok:
        print("OK: no invisible/zero-width characters found in pages.")
    else:
        print(f"\nTotal: {total_hits} line(s) with invisible characters.")
    return ok


def check_pages_no_unicode_dashes(verbose=False) -> bool:
    """Port of check_pages_no_unicode_dashes.sh (en/ and ru/, all modules)."""
    ok = True
    for _, en_root, ru_root in module_roots():
        for root in (en_root, ru_root):
            for f in list(_iter_files(root / "pages", ".adoc")) + list(_iter_files(root / "partials", ".adoc")):
                lines = _read_lines(f)
                if lines is None:
                    continue
                hits = [(i, l) for i, l in enumerate(lines, 1) if EN_EM_DASH_RE.search(l)]
                if hits:
                    ok = False
                    print(f"FILE     {f}")
                    for i, l in hits:
                        print(f"  line {i}: {l}")
    if ok:
        print("OK: no en dash (–) or em dash (—) characters found in pages.")
    return ok


# --------------------------------------------------------------------------
# PAGES: orphaned (not reachable from nav.adoc)
# --------------------------------------------------------------------------

_START_PAGE_RE = re.compile(r'^start_page:\s*(?:([\w-]+):)?(\S+)', re.MULTILINE)
_COMMENT_LINE_ONLY_RE = re.compile(r'^\s*//')


def _strip_comment_lines(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not _COMMENT_LINE_ONLY_RE.match(l))


def _parse_start_page(antora_yml: Path):
    """Returns (module_name, page_rel) for antora.yml's start_page, or
    (None, None) if not found. Antora defaults an unqualified start_page
    (no "module:" prefix) to the ROOT module."""
    antora_text = _read_text(antora_yml)
    if not antora_text:
        return None, None
    m = _START_PAGE_RE.search(antora_text)
    if not m:
        return None, None
    module = m.group(1) or "ROOT"
    return module, m.group(2).strip()


def _combined_nav_text(root: Path) -> str:
    """A module's own nav.adoc plus any include::partial$...[] partials it
    pulls in, comments stripped."""
    nav = root / "nav.adoc"
    nav_text = _read_text(nav)
    if nav_text is None:
        return ""
    parts = [_strip_comment_lines(nav_text)]
    for partial_name in _INCLUDE_PARTIAL_RE.findall(nav_text):
        partial_text = _read_text(root / "partials" / partial_name)
        if partial_text is not None:
            parts.append(_strip_comment_lines(partial_text))
    return "\n".join(parts)


def check_pages_orphaned(verbose=False) -> bool:
    """Port of check_pages_orphaned.sh, generalized for multi-module Antora
    sites: a page is considered reachable if *any* module's nav.adoc (they
    can cross-reference each other, e.g. `xref:other-module:page.adoc[]`)
    contains an xref to it, either bare (same-module/default-component form)
    or module-qualified."""
    ok = True
    modules = list(module_roots())

    for lang_attr in ("en_root", "ru_root"):
        idx = 0 if lang_attr == "en_root" else 1
        lang_roots = {name: (en_root, ru_root)[idx] for name, en_root, ru_root in modules}
        modules_root = EN_MODULES_ROOT if idx == 0 else RU_MODULES_ROOT
        antora_yml = modules_root.parent / "antora.yml"
        start_module, start_page = _parse_start_page(antora_yml)

        # Union of every module's nav (a page can be linked from a sibling
        # module's nav via a component-qualified xref, not just its own).
        combined_nav_text = "\n".join(_combined_nav_text(r) for r in lang_roots.values() if (r / "nav.adoc").is_file())
        if not combined_nav_text:
            continue

        for name, root in lang_roots.items():
            for f in _iter_files(root / "pages", ".adoc"):
                rel = f.relative_to(root / "pages").as_posix()
                if start_module == name and start_page == rel:
                    continue
                if f"xref:{rel}" in combined_nav_text or f"xref:{name}:{rel}" in combined_nav_text:
                    continue
                ok = False
                print(f"ORPHANED  {f}  (not referenced in any nav.adoc)")

    if ok:
        print("OK: all pages are referenced in nav.adoc.")
    return ok


# --------------------------------------------------------------------------
# PAGES: structure parity (EN vs RU)
# --------------------------------------------------------------------------

_STRUCT_LINE_RE = re.compile(
    r'^(=+ |\.[^. ]|----$|\.\.\.\.$|====$|\*\*\*\*$|\|===$|\[.*\]$|include::)'
)
_STRUCT_HEADING_RE = re.compile(r'^(=+) .*')
_STRUCT_BLOCKTITLE_RE = re.compile(r'^\.[^. ].*')


def _structure_skeleton(path: Path):
    lines = _read_lines(path)
    if lines is None:
        return []
    out = []
    for lineno, line in enumerate(lines, 1):
        if not _STRUCT_LINE_RE.match(line):
            continue
        m = _STRUCT_HEADING_RE.match(line)
        if m:
            out.append((lineno, f"{m.group(1)} <heading>"))
            continue
        if _STRUCT_BLOCKTITLE_RE.match(line):
            out.append((lineno, ".<block title>"))
            continue
        out.append((lineno, line))
    return out


def check_pages_structure_parity(verbose=False) -> bool:
    """Port of check_pages_structure_parity.sh."""
    ok = True
    for _, en_root, ru_root in module_roots():
        for subdir in ("pages", "partials"):
            for en_file in _iter_files(en_root / subdir, ".adoc"):
                rel = en_file.relative_to(en_root)
                ru_file = ru_root / rel
                if not ru_file.is_file():
                    print(f"MISSING  {en_file}  (no ru counterpart)")
                    ok = False
                    continue
                if not _compare_skeleton_pair(en_file, ru_file, _structure_skeleton, verbose):
                    ok = False

            for ru_file in _iter_files(ru_root / subdir, ".adoc"):
                rel = ru_file.relative_to(ru_root)
                if not (en_root / rel).is_file():
                    print(f"MISSING  {ru_file}  (no en counterpart)")
                    ok = False

    if ok:
        print("OK: en/ru structure matches for all compared files.")
    return ok


# --------------------------------------------------------------------------
# PAGES: untranslated-line heuristic
# --------------------------------------------------------------------------

_STOPWORDS = (
    "the|is|are|and|or|with|this|that|these|those|you|your|for|from|into|"
    "when|where|which|while|because|however|therefore|then|than|been|have|"
    "has|had|will|would|should|could|can|not|but|also|each|such|only|about|"
    "between|through|before|after|during|without|within|both|either|neither|"
    "more|most|some|any|all|other|same|its|their|our"
)
_STOPWORDS_RE = re.compile(rf'\b(?:{_STOPWORDS})\b')
_PRODUCT_NAMES_RE = re.compile(r'\b(?:CentOS|Ubuntu|Red Hat|RHEL)\b')

_SKIP_ATTR_RE = re.compile(r'^\[.*\]$')
_SKIP_CODESPAN_ITEM_RE = re.compile(r'^[*.\s]+`[^`]+`\s*$')
_SKIP_BOLDITALIC_ITEM_RE = re.compile(r'^[*.\s]+\*_.+_\*:?(\s*\S+)?\s*$')
_SKIP_TABLE_CELL_RE = re.compile(r'^(\.\d+\+)?[a-z]?\|')
_SKIP_ALLCAPS_TITLE_RE = re.compile(r'^\.[^a-z]+$')
_SKIP_FUNC_HEADING_RE = re.compile(r'^=+\s[A-Za-z_][A-Za-z0-9_]*\(.*\)')
_LOWERCASE_RE = re.compile(r'[a-z]')
_HEADING_RE = re.compile(r'^=+\s')

_STRIP_CODE_SPAN_RE = re.compile(r'`[^`]*`')
_STRIP_PLACEHOLDER_RE = re.compile(r'<[^>]*>')
_STRIP_DOUBLE_ANGLE_RE = re.compile(r'<<[^>]*>>')
_STRIP_BRACKET_RE = re.compile(r'\[[^\]]*\]')
_STRIP_PAREN_RE = re.compile(r'\([^)]*\)')
_STRIP_BOLD_RE = re.compile(r'\*\*[^*]*\*\*')
_STRIP_BOLDITALIC_RE = re.compile(r'\*_[^*_]*_\*')
_STRIP_ITALIC_RE = re.compile(r'_[^_]*_')
_STRIP_XREF_RE = re.compile(r'xref:\S*')
_STRIP_URL_RE = re.compile(r'https?://\S*')
_HYPHEN_JOIN_RE = re.compile(r'([a-z])-([a-z])')


def _code_delim_type(line: str):
    if re.match(r'^----\s*$', line):
        return "dash"
    if re.match(r'^\.\.\.\.\s*$', line):
        return "dot"
    if re.match(r'^\+\+\+\+\s*$', line):
        return "plus"  # passthrough block (e.g. [stem] math formulas), not prose
    return None


def _is_skip_line(line: str) -> bool:
    if line.strip() == "":
        return True
    if line.startswith(":"):
        return True
    if _SKIP_ATTR_RE.match(line):
        return True
    if line.startswith("include::"):
        return True
    if line.startswith("//"):
        return True
    if _SKIP_CODESPAN_ITEM_RE.match(line):
        return True
    if _SKIP_BOLDITALIC_ITEM_RE.match(line):
        return True
    if line[:1].isspace():
        return True
    if _SKIP_TABLE_CELL_RE.match(line):
        return True
    if _SKIP_ALLCAPS_TITLE_RE.match(line):
        return True
    if _SKIP_FUNC_HEADING_RE.match(line):
        return True
    if line.endswith("::"):
        return True
    if not _LOWERCASE_RE.search(line):
        return True

    stripped = _STRIP_CODE_SPAN_RE.sub("", line)
    stripped = _STRIP_PLACEHOLDER_RE.sub("", stripped)
    stripped = _PRODUCT_NAMES_RE.sub("", stripped)
    if not _LOWERCASE_RE.search(stripped):
        return True

    return False


def _strip_noise(line: str) -> str:
    s = _STRIP_CODE_SPAN_RE.sub("", line)
    s = _STRIP_DOUBLE_ANGLE_RE.sub("", s)
    s = _STRIP_BRACKET_RE.sub("", s)
    s = _STRIP_PAREN_RE.sub("", s)
    s = _STRIP_BOLD_RE.sub("", s)
    s = _STRIP_BOLDITALIC_RE.sub("", s)
    s = _STRIP_ITALIC_RE.sub("", s)
    s = _STRIP_XREF_RE.sub("", s)
    s = _STRIP_URL_RE.sub("", s)
    return s


def _check_translation_pair(en_file: Path, ru_file: Path, strict: bool, report_header):
    en_lines = _read_lines(en_file)
    ru_lines = _read_lines(ru_file)
    if en_lines is None or ru_lines is None:
        return
    n = min(len(en_lines), len(ru_lines))

    in_code = None
    in_comment_block = False
    in_cell = False
    header_printed = False

    def ensure_header():
        nonlocal header_printed
        if not header_printed:
            report_header(ru_file)
            header_printed = True

    for i in range(n):
        en_line = en_lines[i]
        ru_line = ru_lines[i]
        lineno = i + 1

        if re.match(r'^////\s*$', en_line):
            in_comment_block = not in_comment_block
            continue
        if in_comment_block:
            continue

        delim = _code_delim_type(en_line)
        if delim:
            if in_code == delim:
                in_code = None
            elif in_code is None:
                in_code = delim
            continue
        if in_code:
            continue

        if en_line.startswith("|==="):
            in_cell = False
        elif re.match(r'^(\.\d+\+)?a\|', en_line):
            in_cell = True
        elif _SKIP_TABLE_CELL_RE.match(en_line):
            in_cell = False
        elif in_cell:
            continue

        if _is_skip_line(en_line):
            continue

        if len(en_line.split()) < 3:
            continue

        if en_line == ru_line:
            ensure_header()
            print(f"  UNTRANSLATED  line {lineno}: {en_line}")
        elif strict and not _HEADING_RE.match(en_line):
            candidate = _strip_noise(ru_line).lower()
            candidate = _HYPHEN_JOIN_RE.sub(r'\1\2', candidate)
            if _STOPWORDS_RE.search(candidate):
                ensure_header()
                print(f"  SUSPECT       line {lineno}: {ru_line}")


def check_pages_translation(verbose=False) -> bool:
    """Port of check_pages_translation.sh. `verbose` enables the stricter
    stopword-based heuristic (the script's `-v` flag)."""
    ok = True

    def report_header(ru_file):
        nonlocal ok
        ok = False
        print(f"FILE     {ru_file}")

    for _, en_root, ru_root in module_roots():
        for subdir in ("pages", "partials"):
            for en_file in _iter_files(en_root / subdir, ".adoc"):
                rel = en_file.relative_to(en_root)
                ru_file = ru_root / rel
                if not ru_file.is_file():
                    continue
                _check_translation_pair(en_file, ru_file, verbose, report_header)

    if ok:
        print("OK: no untranslated lines detected.")
    return ok


# --------------------------------------------------------------------------
# CHECK REGISTRY
# --------------------------------------------------------------------------

CHECKS = {
    "examples-no-cyrillic": check_examples_no_cyrillic,
    "examples-orphaned": check_examples_orphaned,
    "examples-parity": check_examples_parity,
    "images-orphaned": check_images_orphaned,
    "nav-structure-parity": check_nav_structure_parity,
    "pages-broken-refs": check_pages_broken_refs,
    "pages-line-parity": check_pages_line_parity,
    "pages-no-cyrillic": check_pages_no_cyrillic,
    "pages-no-invisible-chars": check_pages_no_invisible_chars,
    "pages-no-unicode-dashes": check_pages_no_unicode_dashes,
    "pages-orphaned": check_pages_orphaned,
    "pages-structure-parity": check_pages_structure_parity,
    "pages-translation": check_pages_translation,
}

# Checks whose logic is heuristic (no real AsciiDoc parser behind it) and can
# therefore misfire on legitimate content -- flagged so --list-checks and the
# README can warn people to treat their output as a review list, not a gate.
BETA_CHECKS = {
    "pages-structure-parity",
    "pages-translation",
}


# ==========================================================================
# SYNC: align a RU page's structure/content with its EN counterpart after an
# EN edit. Ported from sync_pages_from_en.py -- see that tool's original
# docstring (preserved below) for the detailed design rationale.
# ==========================================================================
"""
Never touches the EN file. Aligns the RU file's structural "skeleton"
(headings, anchors, delimited blocks, option/flag terms, code lines) to EN's,
and copies in new or changed EN lines verbatim (left untranslated) wherever
RU has nothing corresponding yet. Existing RU prose is never rewritten or
removed -- only technical tokens that must be byte-identical across
languages (flag names, code/command lines, include paths, ids) are corrected
when they've drifted (e.g. a stale `plpythonu` left behind after EN moved to
`plpython3u`).
"""

EN_MARK = "en/modules/"
RU_MARK = "ru/modules/"

DELIM_RE = re.compile(r'^(?:-{2}|-{4,}|\.{4,}|={4,}|\*{4,}|\|={3,})$')
CODE_DELIM_RE = re.compile(r'^(?:-{4,}|\.{4,})$')

HEADING_RE = re.compile(r'^(=+)\s+\S')
ID_RE = re.compile(r'^\[#([\w-]+)]\s*$')
DOCATTR_RE = re.compile(r'^:([\w-]+):')
ATTR_RE = re.compile(r'^\[[^\[\]].*]\s*$')
INCLUDE_RE = re.compile(r'^include::')
BLOCKTITLE_RE = re.compile(r'^\.[^.\s]')
TERM_RE = re.compile(r'^(\[\[[\w-]+])?(.+)::\s*$')
ALL_CAPS_TERM_RE = re.compile(r'^[A-Z][A-Z0-9_]*(\s*,\s*[A-Z][A-Z0-9_]*)*$')
XREF_ITEM_RE = re.compile(r'^[*.]+\s*xref:([^\[]+)\[[^\]]*]\s*$')

CELL_KEY_RE = re.compile(r'^\|([A-Z][A-Z0-9_]+)\b(.*)$')
CELL_KEY_PLACEHOLDER_RE = re.compile(r'''^\s*['"]?<''')
CELL_KEY_CAPS_ONLY_RE = re.compile(r'^[A-Z0-9_,\s]+$')
CELL_LITERAL_RE = re.compile(r'^\|([a-z][a-z0-9_]*|-+)$')


def _is_cell_key(line):
    if CELL_LITERAL_RE.match(line):
        return True
    m = CELL_KEY_RE.match(line)
    if not m:
        return False
    rest = m.group(2)
    if not rest.strip():
        return True
    if CELL_KEY_PLACEHOLDER_RE.match(rest):
        return True
    if '\\|' in rest:
        return True
    if CELL_KEY_CAPS_ONLY_RE.match(rest):
        return True
    return False


COMMENT_IN_CODE_RE = re.compile(r'^\s*(#|--|//)\s')
STALE_MARK_RE = re.compile(r'^// STALE VERSION:')
ORPHAN_MARK_RE = re.compile(r'^// POSSIBLY ORPHANED:')
COMMENT_LINE_RE = re.compile(r'^//')
SYNC_CYRILLIC_RE = re.compile(r'[Ѐ-ӿ]')

FORCE_SYNC_TYPES = {"DELIM", "ID", "ATTR", "INCLUDE", "TERM", "CODE", "CONT", "CELLKEY"}


def sync_classify(line, stack):
    stripped = line.strip()

    if STALE_MARK_RE.match(stripped):
        return ("STALEMARK",)
    if ORPHAN_MARK_RE.match(stripped):
        return ("ORPHANMARK",)

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

    if COMMENT_LINE_RE.match(stripped):
        return ("COMMENT",)

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

    if not SYNC_CYRILLIC_RE.search(line) and _is_cell_key(line):
        return ("CELLKEY", stripped)

    m = TERM_RE.match(line)
    if m:
        content = m.group(2).strip()
        if content.startswith(("-", "`")) or ALL_CAPS_TERM_RE.match(content):
            return ("TERM", line)
        return ("TERMX",)

    return ("PROSE",)


def sync_signatures(lines):
    stack = []
    return [sync_classify(line, stack) for line in lines]


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
    if sig_type in FORCE_SYNC_TYPES and en_l != ru_l and not SYNC_CYRILLIC_RE.search(ru_l):
        replaced.append((ru_l, en_l))
        force_synced.add(en_idx)
        return en_l
    return ru_l


def _front_pair_and_append(en_slice, ru_slice, en_sig_slice, ru_sig_slice, out, inserted, replaced, pairs, en_base, ru_base, force_synced, orphaned):
    ei = ri = 0
    while ei < len(en_slice) and ri < len(ru_slice):
        if ru_sig_slice[ri][0] in ("STALEMARK", "ORPHANMARK"):
            out.append(ru_slice[ri])
            ri += 1
            continue
        en_type, ru_type = en_sig_slice[ei][0], ru_sig_slice[ri][0]
        if en_type != ru_type:
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
                start_pos = len(out)
                out.extend(extra)
                orphaned.append((start_pos, extra))
                ri += prefix_len
                continue
            start_pos = len(out)
            out.append(ru_slice[ri])
            orphaned.append((start_pos, [ru_slice[ri]]))
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
        start_pos = len(out)
        out.extend(extra)
        orphaned.append((start_pos, extra))


def _align_replace_span(en_slice, ru_slice, en_sig_slice, ru_sig_slice, out, inserted, replaced, pairs, en_base, ru_base, force_synced, orphaned):
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
            start_pos = len(out)
            out.extend(extra)
            orphaned.append((start_pos, extra))
        elif tag == "replace":
            _front_pair_and_append(
                en_slice[i1:i2], ru_slice[j1:j2], en_sig_slice[i1:i2], ru_sig_slice[j1:j2],
                out, inserted, replaced, pairs, en_base + i1, ru_base + j1, force_synced, orphaned,
                                                )


def sync_merge(en_lines, ru_lines, pins=None):
    en_sigs = sync_signatures(en_lines)
    ru_sigs = sync_signatures(ru_lines)
    if pins:
        for en_idx, ru_idx in pins.items():
            if en_sigs[en_idx][0] in GENERIC_TYPES and ru_sigs[ru_idx][0] in GENERIC_TYPES:
                en_sigs[en_idx] = ("PINNED", ru_idx)
                ru_sigs[ru_idx] = ("PINNED", ru_idx)
    en_match = matching_signatures(en_sigs, "EN")
    ru_match = matching_signatures(ru_sigs, "RU")
    sm = difflib.SequenceMatcher(a=en_match, b=ru_match, autojunk=False)

    out = []
    inserted = []
    replaced = []
    pairs = []
    force_synced = set()
    orphaned = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.extend(ru_lines[j1:j2])
            for k in range(i2 - i1):
                pairs.append((i1 + k, j1 + k))
        elif tag == "delete":
            new_lines = en_lines[i1:i2]
            out.extend(new_lines)
            inserted.append(new_lines)
        elif tag == "insert":
            extra = ru_lines[j1:j2]
            start_pos = len(out)
            out.extend(extra)
            orphaned.append((start_pos, extra))
        elif tag == "replace":
            _align_replace_span(
                en_lines[i1:i2], ru_lines[j1:j2], en_sigs[i1:i2], ru_sigs[j1:j2],
                out, inserted, replaced, pairs, i1, j1, force_synced, orphaned,
            )

    return out, inserted, replaced, pairs, force_synced, orphaned


def _content_diff_pins(old_en_lines, new_en_lines, ru_lines):
    """Lines unchanged between the EN file's previous and current revision
    can shift position when new content is inserted elsewhere -- e.g. a new
    bullet added before an existing one in an anchor-free list. sync_merge
    then has nothing but raw position to align RU against, and can pair the
    shifted-but-unchanged EN line with the wrong RU line (see pg_depend's
    PARTITION_PRI bullet landing on the pre-existing PIN bullet's RU text).

    Since old-EN-vs-new-EN is a same-language exact-text diff, it can find
    that unchanged content with certainty. Combined with a baseline
    old-EN-to-RU alignment (RU should already mirror old EN structurally
    from the last successful sync), this recovers new-EN-index -> RU-index
    pins for content sync_merge would otherwise have to guess about."""
    if not old_en_lines or not ru_lines:
        return {}
    baseline_pairs = sync_merge(old_en_lines, ru_lines)[3]
    old_to_ru = dict(baseline_pairs)
    pins = {}
    used_ru = set()
    sm = difflib.SequenceMatcher(a=old_en_lines, b=new_en_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            continue
        for k in range(i2 - i1):
            ru_idx = old_to_ru.get(i1 + k)
            if ru_idx is not None and ru_idx not in used_ru:
                pins[j1 + k] = ru_idx
                used_ru.add(ru_idx)
    return pins


def _sync_merge_safe(en_lines, ru_lines, old_en_lines):
    """Runs the plain structural merge first, and only reaches for the
    old-EN-diff pins (see _content_diff_pins) when there's actually
    something to fix. Skipping pins on an already-clean file matters
    because the pins' baseline old-EN<->RU alignment assumes RU still
    mirrors old EN -- if RU was already hand-updated ahead of the tool
    (e.g. a manual fix applied before re-running --sync), that assumption
    breaks and pins can misalign a file that was already fine."""
    plain = sync_merge(en_lines, ru_lines)
    if plain[0] == ru_lines or not old_en_lines:
        return plain
    pins = _content_diff_pins(old_en_lines, en_lines, ru_lines)
    if not pins:
        return plain
    return sync_merge(en_lines, ru_lines, pins=pins)


HUNK_RE = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')


def _last_commit_touching(path: Path):
    result = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", str(path)],
        capture_output=True, text=True,
    )
    sha = result.stdout.strip()
    return sha or None


def _git_show(ref: str, path: Path):
    result = subprocess.run(
        ["git", "show", f"{ref}:{path.as_posix()}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _git_diff_hunks(ref: str, path: Path):
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
    ref = since or _last_commit_touching(ru_path)
    if not ref:
        return None, []

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
            continue
        for k in range(h["new_count"]):
            new_lineno = h["new_start"] + k
            en_idx = new_lineno - 1
            if en_idx in force_synced:
                continue
            ru_idx = en_to_ru.get(en_idx)
            if ru_idx is None:
                continue
            if (ru_idx + 1) in ru_touched:
                continue
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
    marked = list(ru_lines)
    count = 0
    for f in sorted(reworded, key=lambda f: f["ru_lineno"], reverse=True):
        ru_idx = f["ru_lineno"] - 1
        if marked[ru_idx] == f["new_en"]:
            continue
        old_ru = marked[ru_idx]
        marked[ru_idx] = f["new_en"]
        marked.insert(ru_idx + 1, f"// STALE VERSION: {old_ru}")
        count += 1
    return marked, count


ORPHAN_MARKER_TEXT = (
    "// POSSIBLY ORPHANED: no EN counterpart found nearby -- "
    "review whether this was intentionally removed upstream"
)


def _visible_orphan_offsets(block, already_reported):
    return [
        o for o, l in enumerate(block)
        if l.strip() and not STALE_MARK_RE.match(l.strip())
           and not ORPHAN_MARK_RE.match(l.strip())
           and not COMMENT_LINE_RE.match(l.strip()) and l not in already_reported
    ]


def apply_orphan_markers(ru_lines, orphaned, already_reported):
    marked = list(ru_lines)
    positions = set()
    for start_pos, block in orphaned:
        offsets = _visible_orphan_offsets(block, already_reported)
        if offsets:
            positions.add(start_pos + offsets[0])
    count = 0
    for pos in sorted(positions, reverse=True):
        if pos > 0 and ORPHAN_MARK_RE.match(marked[pos - 1].strip()):
            continue
        marked.insert(pos, ORPHAN_MARKER_TEXT)
        count += 1
    return marked, count


def ru_path_for(en_path: Path) -> Path:
    s = str(en_path)
    if EN_MARK not in s:
        sys.exit(f"error: path does not look like an EN page (missing '{EN_MARK}'): {en_path}")
    return Path(s.replace(EN_MARK, RU_MARK, 1))


def run_sync(en_file: str, dry_run: bool, since: str = None):
    en_path = Path(en_file)
    if not en_path.is_file():
        sys.exit(f"error: not a file: {en_path}")

    ru_path = ru_path_for(en_path)
    en_lines = (_read_text(en_path) or "").splitlines()
    ru_existed = ru_path.is_file()

    if ru_existed:
        ru_lines = (_read_text(ru_path) or "").splitlines()
    else:
        print(f"NOTE: {ru_path} does not exist yet -- creating it as a full (untranslated) copy of EN.")
        ru_lines = []

    ref = None
    old_en_lines = None
    if ru_existed:
        ref = since or _last_commit_touching(ru_path)
        if ref:
            old_en_text = _git_show(ref, en_path)
            if old_en_text is not None:
                old_en_lines = old_en_text.splitlines()

    merged, inserted, replaced, pairs, force_synced, orphaned = _sync_merge_safe(en_lines, ru_lines, old_en_lines)

    reworded, marked = [], 0
    if ru_existed:
        ref, reworded = find_reworded_lines(en_path, ru_path, en_lines, ru_lines, pairs, force_synced, since=ref)
        if reworded:
            ru_lines_marked, marked = apply_stale_markers(ru_lines, reworded)
            if marked:
                merged, inserted, replaced, pairs, force_synced, orphaned = _sync_merge_safe(en_lines, ru_lines_marked, old_en_lines)

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

    merged, orphan_marked = apply_orphan_markers(merged, orphaned, already_reported)

    structurally_synced = merged == ru_lines

    if structurally_synced:
        print(f"OK: {ru_path} already matches the EN structure/content; nothing to do structurally.")
    elif dry_run:
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

    real_inserted = []
    for block in inserted:
        visible = [l for l in block if not COMMENT_LINE_RE.match(l.strip())]
        if any(l.strip() for l in visible):
            real_inserted.append(visible)

    if real_inserted:
        total = sum(len(b) for b in real_inserted)
        print(f"\nInserted {total} new line(s) from EN across {len(real_inserted)} block(s), left untranslated:")
        for block in real_inserted:
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
                continue
            print(f"\n  EN:{f['lineno']} / RU:{f['ru_lineno']}")
            print(f"    {f['new_en']}")
            print(f"    // STALE VERSION: {f['ru']}")

    if ru_existed and ref is None:
        print(f"\nNOTE: no git history found for {ru_path}; skipped the reworded-line check.")

    real_orphaned = []
    for _, block in orphaned:
        visible = [block[o] for o in _visible_orphan_offsets(block, already_reported)]
        if visible:
            real_orphaned.append(visible)
    if real_orphaned:
        total = sum(len(b) for b in real_orphaned)
        print(f"\nPOSSIBLY ORPHANED: {total} RU line(s) across {len(real_orphaned)} block(s) have no EN counterpart")
        print("anywhere nearby (left in place, not deleted -- review whether EN removed this on purpose).")
        if orphan_marked:
            print(f"Marked {orphan_marked} of them with a `// POSSIBLY ORPHANED:` comment right before the block, "
                  "so it's visible directly in the file:")
        for block in real_orphaned:
            for l in block:
                print(f"  ? {l}")
            print()

    if real_inserted or replaced or marked:
        print("\nNext: run ./docs_tool.py --check-pages-translation to locate the newly untranslated lines for translation.")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    check_group = parser.add_argument_group("checks")
    for name in CHECKS:
        check_group.add_argument(f"--check-{name}", action="store_true", help=argparse.SUPPRESS)

    parser.add_argument("--all-checks", action="store_true", help="Run every check.")
    parser.add_argument("--list-checks", action="store_true", help="List available --check-* flags and exit.")
    parser.add_argument("--list-modules", action="store_true",
                        help="List every discovered module (under en/modules/ and ru/modules/) and exit.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose mode: show diffs (parity checks) or enable the stricter "
                             "stopword heuristic (--check-pages-translation).")
    parser.add_argument("--external-root", action="append", metavar="NAME=PATH",
                        help="With --check-pages-broken-refs: resolve xref:/include:: targets "
                             "against another Antora component's repo checked out locally, e.g. "
                             "--external-root ADCM=../docs-adcm. Repeatable. Without this, "
                             "references into a component that isn't part of this repo are left "
                             "unchecked rather than reported broken.")

    sync_group = parser.add_argument_group("sync")
    sync_group.add_argument("--sync", metavar="EN_FILE",
                            help="(beta) Align the RU counterpart of EN_FILE to match its current "
                                 "structure/content. Heuristic aligner, not a semantic merge -- review its "
                                 "output before trusting it.")
    sync_group.add_argument("-n", "--dry-run", action="store_true",
                            help="With --sync: print the diff instead of writing the RU file.")
    sync_group.add_argument("--since", metavar="REF",
                            help="With --sync: git ref to diff the EN file against when looking for "
                                 "reworded (not just added) lines (default: the last commit that touched the RU file).")
    return parser


def main():
    global EXTERNAL_COMPONENTS
    parser = build_parser()
    args = parser.parse_args()
    EXTERNAL_COMPONENTS = _load_external_components(args.external_root)

    if args.list_checks:
        for name in CHECKS:
            tag = " (beta)" if name in BETA_CHECKS else ""
            print(f"--check-{name}{tag}")
        if any(name in BETA_CHECKS for name in CHECKS):
            print("\n(beta): heuristic, not a real AsciiDoc parser -- treat findings as a "
                  "review list, not a hard failure.")
        return

    if args.list_modules:
        for name in discover_module_names():
            print(name)
        return

    if args.sync:
        run_sync(args.sync, dry_run=args.dry_run, since=args.since)
        return

    selected = list(CHECKS) if args.all_checks else [
        name for name in CHECKS if getattr(args, f"check_{name.replace('-', '_')}")
    ]

    if not selected:
        parser.print_help()
        sys.exit(2)

    overall_ok = True
    for i, name in enumerate(selected):
        if len(selected) > 1:
            if i:
                print()
            print(f"=== --check-{name} ===")
        if not CHECKS[name](verbose=args.verbose):
            overall_ok = False

    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
