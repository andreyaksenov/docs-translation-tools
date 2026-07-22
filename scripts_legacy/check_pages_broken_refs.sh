#!/usr/bin/env bash
set -u

status=0

report() {
    printf 'BROKEN   %s:%d  %s\n' "$1" "$2" "$3"
    status=1
}

# A page's anchors aren't necessarily in the page's own source: pages like
# guc_reference.adoc are assembled entirely from include::partial$...[]
# partials, so anchor lookup has to follow those includes (recursively, with
# a depth cap as a guard against any accidental include cycle).
collect_include_partials() {
    local file="$1" root="$2" depth="${3:-0}"
    (( depth > 5 )) && return
    echo "$file"
    local partial_name
    while IFS= read -r partial_name; do
        local partial_file="$root/partials/$partial_name"
        [[ -f "$partial_file" ]] && collect_include_partials "$partial_file" "$root" "$(( depth + 1 ))"
    done < <(grep -oE 'include::partial\$[^[]+\.adoc' "$file" 2>/dev/null | sed -E 's#include::partial\$##')
}

# Anchors are declared as their own line, either "[#id]" or "[[id]]" /
# "[[id,xreflabel]]".
anchor_exists() {
    local target_file="$1" id="$2" root="$3" f
    while IFS= read -r f; do
        grep -qE '^\[#'"$id"'\]$|^\[\['"$id"'(,|\]\])' "$f" 2>/dev/null && return 0
    done < <(collect_include_partials "$target_file" "$root")
    return 1
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

                local fragment=""
                if [[ "$target" == *#* ]]; then
                    fragment="${target#*#}"
                    target="${target%%#*}"
                fi

                if [[ "$target" == *.adoc ]]; then
                    local target_file="$root/pages/$target"
                    if [[ ! -f "$target_file" ]]; then
                        report "$file" "$lineno" "xref:$target"
                    elif [[ -n "$fragment" ]] && ! anchor_exists "$target_file" "$fragment" "$root"; then
                        report "$file" "$lineno" "xref:$target#$fragment (anchor not found)"
                    fi
                elif [[ -n "$target" ]]; then
                    # No .adoc suffix: a same-page anchor reference.
                    anchor_exists "$file" "$target" "$root" || report "$file" "$lineno" "xref:$target (anchor not found)"
                fi
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
