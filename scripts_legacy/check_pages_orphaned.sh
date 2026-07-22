#!/usr/bin/env bash
set -u

status=0

check_lang() {
    local root="$1"   # e.g. en/modules/ROOT
    local nav="$root/nav.adoc"

    # The module's start_page (antora.yml) is the landing page and is not
    # expected to appear in the nav sidebar.
    local start_page
    start_page=$(grep -E '^start_page:' "${root%/modules/ROOT}/antora.yml" 2>/dev/null \
        | sed -E 's/^start_page:[[:space:]]*(ROOT:)?//' | tr -d '[:space:]')

    # nav.adoc pulls in some sections via include::partial$X.adoc[] (e.g. the
    # SQL command / utility reference lists), so the xrefs to check against
    # live partly in those partials, not just in nav.adoc itself.
    local nav_text
    nav_text=$(grep -vE '^[[:space:]]*//' "$nav")
    while IFS= read -r partial_name; do
        nav_text+=$'\n'"$(grep -vE '^[[:space:]]*//' "$root/partials/$partial_name" 2>/dev/null)"
    done < <(grep -oE 'include::partial\$[^[]+\.adoc' "$nav" | sed -E 's#include::partial\$##')

    while IFS= read -r -d '' file; do
        rel="${file#"$root"/pages/}"
        [[ "$rel" == "$start_page" ]] && continue
        if ! grep -qF "xref:$rel" <<< "$nav_text"; then
            printf 'ORPHANED  %s  (not referenced in %s)\n' "$file" "$nav"
            status=1
        fi
    done < <(find "$root/pages" -type f -name '*.adoc' -print0)
}

check_lang en/modules/ROOT
check_lang ru/modules/ROOT

if [[ $status -eq 0 ]]; then
    echo "OK: all pages are referenced in nav.adoc."
fi

exit $status