#!/usr/bin/env bash
set -u

EN_ROOT="en/modules/ROOT"
RU_ROOT="ru/modules/ROOT"

status=0

compare_tree() {
    local subdir="$1"   # pages or partials
    local ext="$2"      # adoc

    while IFS= read -r -d '' en_file; do
        rel="${en_file#"$EN_ROOT"/}"
        ru_file="$RU_ROOT/$rel"

        if [[ ! -f "$ru_file" ]]; then
            printf 'MISSING  %s  (no ru counterpart)\n' "$en_file"
            status=1
            continue
        fi

        en_lines=$(wc -l < "$en_file" | tr -d '[:space:]')
        ru_lines=$(wc -l < "$ru_file" | tr -d '[:space:]')

        if [[ "$en_lines" -ne "$ru_lines" ]]; then
            printf 'DIFF     %s  (%s lines)\n' "$en_file" "$en_lines"
            printf '         %s  (%s lines)\n' "$ru_file" "$ru_lines"
            status=1
        fi
    done < <(find "$EN_ROOT/$subdir" -type f -name "*.$ext" -print0)

    # Also flag ru files that have no en counterpart
    while IFS= read -r -d '' ru_file; do
        rel="${ru_file#"$RU_ROOT"/}"
        if [[ ! -f "$EN_ROOT/$rel" ]]; then
            printf 'MISSING  %s  (no en counterpart)\n' "$ru_file"
            status=1
        fi
    done < <(find "$RU_ROOT/$subdir" -type f -name "*.$ext" -print0)
}

compare_tree pages adoc
compare_tree partials adoc

if [[ $status -eq 0 ]]; then
    echo "OK: all compared en/ru pages have matching line counts."
fi

exit $status