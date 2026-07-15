#!/usr/bin/env bash
set -u

status=0
verbose=0
[[ "${1:-}" == "-v" ]] && verbose=1

# Extract the structural skeleton of a nav file: list depth (count of
# leading '*'), plus an xref target or include:: target where present.
# Everything else (translated labels, category text) is dropped so only
# structure -- not wording -- is compared. Each line keeps its number for
# easier lookup in -v mode.
#
# Branches are mutually exclusive elif's (not chained sed substitutions):
# chaining would let a later catch-all pattern re-match and overwrite a
# line an earlier pattern already rewrote (e.g. every xref target silently
# collapsing to a generic placeholder), masking real target/order changes.
skeleton() {
    local lineno=0 line
    while IFS= read -r line; do
        lineno=$(( lineno + 1 ))
        if [[ "$line" =~ ^(\*+)[[:space:]]\+\+\+\<svg\>\<use\ xlink:href=\"[^\"]*#([^\"]+)\" ]]; then
            printf '%d:%s <svg:%s>\n' "$lineno" "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
        elif [[ "$line" =~ ^(\*+)[[:space:]]xref:([^\[]+)\[ ]]; then
            printf '%d:%s xref:%s\n' "$lineno" "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
        elif [[ "$line" =~ ^(\*+)[[:space:]] ]]; then
            printf '%d:%s <text>\n' "$lineno" "${BASH_REMATCH[1]}"
        elif [[ "$line" =~ ^include::(.*)$ ]]; then
            printf '%d:include::%s\n' "$lineno" "${BASH_REMATCH[1]}"
        fi
    done < "$1"
}

compare_pair() {
    local en_file="$1"
    local ru_file="$2"

    local en_skel ru_skel
    en_skel=$(skeleton "$en_file" | sed 's/^[0-9]*://')
    ru_skel=$(skeleton "$ru_file" | sed 's/^[0-9]*://')

    if [[ "$en_skel" != "$ru_skel" ]]; then
        printf 'DIFF     %s\n' "$en_file"
        printf '         %s\n' "$ru_file"
        status=1
        if (( verbose )); then
            diff -U1 <(skeleton "$en_file") <(skeleton "$ru_file") \
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

check_lang_pair() {
    local en_root="$1" ru_root="$2"

    compare_pair "$en_root/nav.adoc" "$ru_root/nav.adoc"

    # nav.adoc pulls in some sections via include::partial$X.adoc[]; those
    # partials have their own list structure that should match too.
    while IFS= read -r partial_name; do
        [[ -f "$en_root/partials/$partial_name" && -f "$ru_root/partials/$partial_name" ]] || continue
        compare_pair "$en_root/partials/$partial_name" "$ru_root/partials/$partial_name"
    done < <(grep -oE 'include::partial\$[^[]+\.adoc' "$en_root/nav.adoc" | sed -E 's#include::partial\$##')
}

check_lang_pair en/modules/ROOT ru/modules/ROOT

if [[ $status -eq 0 ]]; then
    echo "OK: nav structure matches for en/ru."
fi

exit $status