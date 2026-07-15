#!/usr/bin/env bash
set -u

EN_ROOT="en/modules/ROOT/examples"
RU_ROOT="ru/modules/ROOT/examples"

status=0
verbose=0
[[ "${1:-}" == "-v" ]] && verbose=1

while IFS= read -r -d '' en_file; do
    rel="${en_file#"$EN_ROOT"/}"
    ru_file="$RU_ROOT/$rel"

    if [[ ! -f "$ru_file" ]]; then
        printf 'MISSING  %s  (no ru counterpart)\n' "$en_file"
        status=1
        continue
    fi

    if [[ "$en_file" == *.sql ]]; then
        # .sql files legitimately have translated comments, so only require
        # matching line counts, not byte-identical content.
        en_lines=$(wc -l < "$en_file" | tr -d '[:space:]')
        ru_lines=$(wc -l < "$ru_file" | tr -d '[:space:]')
        if [[ "$en_lines" -ne "$ru_lines" ]]; then
            printf 'DIFF     %s  (%s lines)\n' "$en_file" "$en_lines"
            printf '         %s  (%s lines)\n' "$ru_file" "$ru_lines"
            status=1
        fi
        continue
    fi

    # Everything else (data/config files) must match byte-for-byte.
    if ! diff -q "$en_file" "$ru_file" > /dev/null 2>&1; then
        printf 'DIFF     %s\n' "$en_file"
        printf '         %s\n' "$ru_file"
        status=1
        if (( verbose )); then
            diff -U1 "$en_file" "$ru_file" | sed -E \
                -e '/^(---|\+\+\+) /d' \
                -e "s|^-|- $en_file:  |" \
                -e "s|^\+|+ $ru_file:  |" \
                -e 's/^ /      /' \
            | sed 's/^/    /'
            echo
        fi
    fi
done < <(find "$EN_ROOT" -type f -print0)

while IFS= read -r -d '' ru_file; do
    rel="${ru_file#"$RU_ROOT"/}"
    if [[ ! -f "$EN_ROOT/$rel" ]]; then
        printf 'MISSING  %s  (no en counterpart)\n' "$ru_file"
        status=1
    fi
done < <(find "$RU_ROOT" -type f -print0)

if [[ $status -eq 0 ]]; then
    echo "OK: en/ru examples match."
fi

exit $status