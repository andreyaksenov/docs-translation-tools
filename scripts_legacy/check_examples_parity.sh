#!/usr/bin/env bash
set -u

EN_ROOT="en/modules/ROOT/examples"
RU_ROOT="ru/modules/ROOT/examples"

# Prints a .sql file with comment-only lines (-- line comments and /* ... */
# blocks) blanked out, so translated comments don't count as content drift
# while actual code lines are still compared verbatim (incl. indentation).
blank_comments() {
    awk '
        {
            line = $0
            trimmed = line
            sub(/^[ \t]+/, "", trimmed)
            sub(/[ \t]+$/, "", trimmed)

            if (in_comment) {
                print ""
                if (index(trimmed, "*/") > 0) in_comment = 0
                next
            }

            if (trimmed == "" || trimmed ~ /^--/) {
                print ""
                next
            }

            if (trimmed ~ /^\/\*/) {
                print ""
                if (index(trimmed, "*/") == 0) in_comment = 1
                next
            }

            # Strip a translated trailing inline comment ("...  -- text"),
            # requiring whitespace on both sides of "--" so it does not
            # touch dash runs inside string literals (e.g. "-----BEGIN...").
            sub(/[ \t]+--([ \t].*)?$/, "", line)
            print line
        }
    ' "$1"
}

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
        # .sql files legitimately have translated comments, so blank out
        # comment-only lines (-- line comments and /* ... */ blocks) before
        # comparing; actual code lines must still match exactly.
        if ! diff -q <(blank_comments "$en_file") <(blank_comments "$ru_file") > /dev/null 2>&1; then
            printf 'DIFF     %s\n' "$en_file"
            printf '         %s\n' "$ru_file"
            status=1
            if (( verbose )); then
                diff -U1 <(blank_comments "$en_file") <(blank_comments "$ru_file") | sed -E \
                    -e '/^(---|\+\+\+) /d' \
                    -e "s|^-|- $en_file:  |" \
                    -e "s|^\+|+ $ru_file:  |" \
                    -e 's/^ /      /' \
                | sed 's/^/    /'
                echo
            fi
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
done < <(find "$EN_ROOT" -type f -not -path "$EN_ROOT/_demo_cluster/*" -print0)

while IFS= read -r -d '' ru_file; do
    rel="${ru_file#"$RU_ROOT"/}"
    if [[ ! -f "$EN_ROOT/$rel" ]]; then
        printf 'MISSING  %s  (no en counterpart)\n' "$ru_file"
        status=1
    fi
done < <(find "$RU_ROOT" -type f -not -path "$RU_ROOT/_demo_cluster/*" -print0)

if [[ $status -eq 0 ]]; then
    echo "OK: en/ru examples match."
fi

exit $status