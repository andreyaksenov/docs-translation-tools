#!/usr/bin/env bash
set -u

EN_ROOT="en/modules/ROOT"
RU_ROOT="ru/modules/ROOT"

status=0
verbose=0
[[ "${1:-}" == "-v" ]] && verbose=1

# Extract the structural skeleton of an .adoc file:
#   - headings: keep only the '=' level marker (titles are translated)
#   - block titles: normalize to '.<title>' marker only
#   - delimited blocks: ----, ...., ====, ****, |===
#   - block attribute lines: [source,sql], [NOTE], etc.
#   - include:: directives (with their full target path)
# Each line is prefixed with its line number for easier lookup.
skeleton() {
    grep -nE '^(=+ |\.[^. ]|----$|\.\.\.\.$|====$|\*\*\*\*$|\|===$|\[|include::)' "$1" \
    | sed -E \
        -e 's/^([0-9]+:=+) .*/\1 <heading>/' \
        -e 's/^([0-9]+:)\.[^. ].*/\1.<block title>/'
}

compare_pair() {
    local rel="$1"
    local en_file="$EN_ROOT/$rel"
    local ru_file="$RU_ROOT/$rel"

    # Compare skeletons ignoring line numbers first
    local en_skel ru_skel
    en_skel=$(skeleton "$en_file" | sed 's/^[0-9]*://')
    ru_skel=$(skeleton "$ru_file" | sed 's/^[0-9]*://')

    if [[ "$en_skel" != "$ru_skel" ]]; then
        printf 'DIFF     %s\n' "$en_file"
        printf '         %s\n' "$ru_file"
        status=1
        if [[ $verbose -eq 1 ]]; then
            # Rewrite diff lines into clickable file:line references:
            #   '-' lines come from the en file, '+' lines from the ru file,
            #   context lines show only :line markers to reduce noise.
            diff -U1 \
                <(skeleton "$en_file") <(skeleton "$ru_file") \
            | sed -E \
                -e '/^(---|\+\+\+) /d' \
                -e "s|^-([0-9]+):|- $en_file:\1  |" \
                -e "s|^\+([0-9]+):|+ $ru_file:\1  |" \
                -e "s|^ ([0-9]+):|      :\1  |" \
            | sed 's/^/    /'
            echo
        fi
    fi
}

check_tree() {
    local subdir="$1"

    while IFS= read -r -d '' en_file; do
        rel="${en_file#"$EN_ROOT"/}"
        if [[ ! -f "$RU_ROOT/$rel" ]]; then
            printf 'MISSING  %s  (no ru counterpart)\n' "$en_file"
            status=1
            continue
        fi
        compare_pair "$rel"
    done < <(find "$EN_ROOT/$subdir" -type f -name '*.adoc' -print0)

    while IFS= read -r -d '' ru_file; do
        rel="${ru_file#"$RU_ROOT"/}"
        if [[ ! -f "$EN_ROOT/$rel" ]]; then
            printf 'MISSING  %s  (no en counterpart)\n' "$ru_file"
            status=1
        fi
    done < <(find "$RU_ROOT/$subdir" -type f -name '*.adoc' -print0)
}

check_tree pages
check_tree partials

if [[ $status -eq 0 ]]; then
    echo "OK: en/ru structure matches for all compared files."
fi

exit $status
