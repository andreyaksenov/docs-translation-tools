#!/usr/bin/env bash
set -u

status=0

check_lang() {
    local root="$1"   # e.g. en/modules/ROOT

    while IFS= read -r -d '' file; do
        hits=$(perl -CSD -ne 'print "$.:$_" if /[\x{2013}\x{2014}]/' "$file" 2>/dev/null)
        [[ -n "$hits" ]] || continue
        printf 'FILE     %s\n' "$file"
        printf '%s\n' "$hits" | sed -E 's/^([0-9]+):/  line \1: /'
        status=1
    done < <(find "$root/pages" "$root/partials" -type f -name '*.adoc' -print0)
}

check_lang en/modules/ROOT
check_lang ru/modules/ROOT

if [[ $status -eq 0 ]]; then
    echo "OK: no en dash (–) or em dash (—) characters found in pages."
fi

exit $status