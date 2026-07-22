#!/usr/bin/env bash
set -u

status=0

check_lang() {
    local root="$1"   # e.g. en/modules/ROOT

    while IFS= read -r -d '' file; do
        rel="${file#"$root"/examples/}"
        if ! grep -qFrl "example\$$rel" "$root/pages" "$root/partials" 2>/dev/null; then
            printf 'ORPHANED  %s  (not included in any pages/partials)\n' "$file"
            status=1
        fi
    done < <(find "$root/examples" -type f -print0)
}

check_lang en/modules/ROOT
check_lang ru/modules/ROOT

if [[ $status -eq 0 ]]; then
    echo "OK: all examples are included somewhere."
fi

exit $status