#!/usr/bin/env bash
set -u

status=0

report() {
    printf 'BROKEN   %s:%d  %s\n' "$1" "$2" "$3"
    status=1
}

# Lines to ignore when scanning for references: asciidoc comments, and
# anything inside a ---- / .... literal/listing block (illustrative syntax
# shown as an example, not live markup).
excluded_lines() {
    awk '
        /^[[:space:]]*\/\// { print NR; next }
        /^(----|\.\.\.\.)[[:space:]]*$/ { in_code = !in_code; print NR; next }
        in_code { print NR }
    ' "$1"
}

check_file() {
    local file="$1" root="$2"
    local dir
    dir="$(dirname "$file")"

    local excluded
    excluded=$'\n'"$(excluded_lines "$file")"$'\n'

    local lineno target
    while IFS=: read -r lineno target; do
        [[ "$excluded" == *$'\n'"$lineno"$'\n'* ]] && continue

        case "$target" in
            xref:*)
                target="${target#xref:}"
                target="${target#ROOT:}"
                # An empty component/module segment before a colon (e.g. the leading
                # ":" in "xref::page.adoc", or "ROOT:" already stripped above) means
                # "current component/module" per Antora's resource-ID grammar, not an
                # error, so it's stripped rather than treated as a broken target.
                while [[ "$target" == :* ]]; do target="${target#:}"; done
                # A real component identifier before the colon (blog::x,
                # product-releases:ROOT:x) means a different, external component,
                # which isn't resolvable here.
                [[ "$target" =~ ^[A-Za-z][A-Za-z0-9_-]*: ]] && continue
                target="${target%%#*}"
                [[ "$target" == *.adoc ]] || continue  # same-page anchor, not a file reference
                [[ -f "$root/pages/$target" ]] || report "$file" "$lineno" "xref:$target"
                ;;
            include::partial\$*)
                target="${target#include::partial\$}"
                [[ -f "$root/partials/$target" ]] || report "$file" "$lineno" "include::partial\$$target"
                ;;
            include::example\$*)
                target="${target#include::example\$}"
                [[ -f "$root/examples/$target" ]] || report "$file" "$lineno" "include::example\$$target"
                ;;
            include::*)
                target="${target#include::}"
                [[ -f "$dir/$target" ]] || report "$file" "$lineno" "include::$target"
                ;;
            injectSvg::*)
                target="${target#injectSvg::}"
                [[ -f "$root/images/$target" ]] || report "$file" "$lineno" "injectSvg::$target"
                ;;
            injectSvg:*)
                target="${target#injectSvg:}"
                [[ -f "$root/images/$target" ]] || report "$file" "$lineno" "injectSvg:$target"
                ;;
        esac
    done < <(grep -noE '(xref:|include::|injectSvg:{1,2})[^][:space:]]+\[' "$file" | sed -E 's/\[$//')
}

check_lang() {
    local root="$1"   # e.g. en/modules/ROOT

    while IFS= read -r -d '' file; do
        check_file "$file" "$root"
    done < <(find "$root/pages" "$root/partials" -type f -name '*.adoc' -print0)
}

check_lang en/modules/ROOT
check_lang ru/modules/ROOT

if [[ $status -eq 0 ]]; then
    echo "OK: no broken xref/include/image references found."
fi

exit $status
