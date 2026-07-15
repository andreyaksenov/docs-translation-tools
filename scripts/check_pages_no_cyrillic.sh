#!/usr/bin/env bash
set -u

EN_ROOT="en/modules/ROOT"

status=0

while IFS= read -r -d '' file; do
    hits=$(grep -nP '[\x{0400}-\x{04FF}]' "$file" 2>/dev/null) || continue
    printf 'FILE     %s\n' "$file"
    printf '%s\n' "$hits" | sed -E 's/^([0-9]+):/  line \1: /'
    status=1
done < <(find "$EN_ROOT/pages" "$EN_ROOT/partials" -type f -name '*.adoc' -print0)

if [[ $status -eq 0 ]]; then
    echo "OK: no Cyrillic characters found in en/ pages."
fi

exit $status