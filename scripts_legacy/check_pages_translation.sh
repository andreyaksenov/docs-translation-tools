#!/usr/bin/env bash
set -u

EN_ROOT="en/modules/ROOT"
RU_ROOT="ru/modules/ROOT"

status=0
strict=0
[[ "${1:-}" == "-v" ]] && strict=1

# Common English stopwords that signal a RU line is still (partly) English.
# [[:<:]]/[[:>:]] are BSD-regex word boundaries (bash's \b is not supported
# on macOS's default bash 3.2 / BSD regex engine).
STOPWORDS_RE='[[:<:]](the|is|are|and|or|with|this|that|these|those|you|your|for|from|into|when|where|which|while|because|however|therefore|then|than|been|have|has|had|will|would|should|could|can|not|but|also|each|such|only|about|between|through|before|after|during|without|within|both|either|neither|more|most|some|any|all|other|same|its|their|our)[[:>:]]'

# Proper nouns that never get translated; a line consisting only of one of
# these plus a version number/punctuation (e.g. "- CentOS 7.9.") is not
# untranslated prose and shouldn't be flagged.
PRODUCT_NAMES_RE='CentOS|Ubuntu|Red Hat|RHEL'

# Which literal/listing block, if any, we're currently inside: "" (none),
# "dash" (----) or "dot" (....). ====, ****, |=== are prose containers
# (example, sidebar, table) and don't toggle this.
code_delim_type() {
    if [[ "$1" =~ ^----[[:space:]]*$ ]]; then
        echo dash
    elif [[ "$1" =~ ^\.\.\.\.[[:space:]]*$ ]]; then
        echo dot
    fi
}

is_skip_line() {
    local line="$1"
    [[ -z "${line// /}" ]] && return 0                 # blank
    [[ "$line" =~ ^: ]] && return 0                     # :attr: value
    [[ "$line" =~ ^\[.*\]$ ]] && return 0                # [source,sql] etc.
    [[ "$line" =~ ^include:: ]] && return 0              # include:: directive
    [[ "$line" =~ ^// ]] && return 0                     # asciidoc comment
    [[ "$line" =~ ^[*.\ ]+\`[^\`]+\`\ *$ ]] && return 0  # list item that's only a code span
    [[ "$line" =~ ^[[:space:]] ]] && return 0            # indented literal/continuation paragraph
    [[ "$line" =~ ^(\.[0-9]+\+)?[a-z]?\| ]] && return 0  # table cell/row, incl. a| / .6+a| cell-type markers
    [[ "$line" =~ ^\.[^a-z]+$ ]] && return 0             # all-caps block title (.CREATE FOREIGN TABLE)
    [[ "$line" =~ ^=+\ [A-Za-z_][A-Za-z0-9_]*\(.*\) ]] && return 0  # function-name heading (json_object(...))
    [[ "$line" =~ ::$ ]] && return 0                     # definition-list term (flag, function sig, keyword)
    [[ "$line" =~ [a-z] ]] || return 0                   # no lowercase letters anywhere -> SQL/CLI syntax, not prose

    # Same check, but with code spans (`...`) and syntax-diagram placeholders
    # (<...>) stripped first: catches lines that are pure syntax/enum lists
    # once you discount the inline code and placeholder names, e.g.
    # "* `id` -- `INT`;" or "CONNECTION LIMIT <connlimit> +".
    local stripped
    stripped=$(sed -E -e 's/`[^`]*`//g' -e 's/<[^>]*>//g' -e "s/[[:<:]](${PRODUCT_NAMES_RE})[[:>:]]//g" <<< "$line")
    [[ "$stripped" =~ [a-z] ]] || return 0

    return 1
}

word_count() {
    wc -w <<< "$1" | tr -d '[:space:]'
}

# Strip code spans, internal xrefs (<<anchor,label>>), bracketed link
# labels, parenthetical asides, bold/italic markup, xref targets, and URLs
# before the stopword check: SQL keywords (`WITH`, `ORDER BY`), link titles
# (xref:x.adoc[The title]), English glosses ("(Most Common Values, MCVs)"),
# and hyphenated anchors/paths (#sql-for-update-share, VACUUM-FOR-WRAPAROUND)
# are legitimately left in English and shouldn't count as evidence that a
# RU line is untranslated.
strip_noise() {
    sed -E \
        -e 's/`[^`]*`//g' \
        -e 's/<<[^>]*>>//g' \
        -e 's/\[[^]]*\]//g' \
        -e 's/\([^)]*\)//g' \
        -e 's/\*\*[^*]*\*\*//g' \
        -e 's/\*_[^*_]*_\*//g' \
        -e 's/_[^_]*_//g' \
        -e 's#xref:[^][:space:]]*##g' \
        -e 's#https?://[^][:space:]]*##g' \
        <<< "$1"
}

check_pair() {
    local rel="$1"
    local en_file="$EN_ROOT/$rel"
    local ru_file="$RU_ROOT/$rel"

    local en_lines=() ru_lines=() line
    while IFS= read -r line || [[ -n "$line" ]]; do
        en_lines+=("$line")
    done < "$en_file"
    while IFS= read -r line || [[ -n "$line" ]]; do
        ru_lines+=("$line")
    done < "$ru_file"

    local n=${#en_lines[@]}
    (( ${#ru_lines[@]} < n )) && n=${#ru_lines[@]}

    local in_code="" in_cell=0 header_printed=0 i lineno en_line ru_line wc delim

    for (( i = 0; i < n; i++ )); do
        en_line="${en_lines[$i]}"
        ru_line="${ru_lines[$i]}"
        lineno=$(( i + 1 ))

        delim=$(code_delim_type "$en_line")
        if [[ -n "$delim" ]]; then
            # Only a delimiter matching the currently open block type closes
            # it; a mismatched one (e.g. a literal "----" table border
            # inside a .... block) is just content and must not desync the
            # toggle.
            if [[ "$in_code" == "$delim" ]]; then
                in_code=""
            elif [[ -z "$in_code" ]]; then
                in_code="$delim"
            fi
            continue
        fi
        [[ -n "$in_code" ]] && continue

        # An `a|` cell holds multiple asciidoc paragraphs (blank-line
        # separated); only the first carries the marker, so track it
        # statefully until the next cell/row or the table's closing |===.
        if [[ "$en_line" =~ ^\|=== ]]; then
            in_cell=0
        elif [[ "$en_line" =~ ^(\.[0-9]+\+)?a\| ]]; then
            in_cell=1
        elif [[ "$en_line" =~ ^(\.[0-9]+\+)?[a-z]?\| ]]; then
            in_cell=0
        elif (( in_cell )); then
            continue
        fi

        is_skip_line "$en_line" && continue

        wc=$(word_count "$en_line")
        (( wc < 3 )) && continue

        if [[ "$en_line" == "$ru_line" ]]; then
            if (( ! header_printed )); then
                printf 'FILE     %s\n' "$ru_file"
                header_printed=1
                status=1
            fi
            printf '  UNTRANSLATED  line %d: %s\n' "$lineno" "$en_line"
        elif (( strict )) && [[ ! "$en_line" =~ ^=+[[:space:]] ]] \
            && [[ "$(sed -E 's/([a-z])-([a-z])/\1\2/g' <<< "$(tr '[:upper:]' '[:lower:]' <<< "$(strip_noise "$ru_line")")")" =~ $STOPWORDS_RE ]]; then
            # Hyphenated technical compounds (not-null, master-only,
            # index-only) are joined before matching, since the hyphen
            # otherwise counts as a word boundary and half the compound
            # (not/only) alone looks like a stray English stopword.
            # Headings routinely name a bare SQL clause (=== WHERE clause)
            # even once properly translated, so they're excluded here; a
            # fully-untranslated heading is still caught by the exact-match
            # check above regardless of this flag.
            if (( ! header_printed )); then
                printf 'FILE     %s\n' "$ru_file"
                header_printed=1
                status=1
            fi
            printf '  SUSPECT       line %d: %s\n' "$lineno" "$ru_line"
        fi
    done
}

check_tree() {
    local subdir="$1"
    while IFS= read -r -d '' en_file; do
        rel="${en_file#"$EN_ROOT"/}"
        [[ -f "$RU_ROOT/$rel" ]] || continue
        check_pair "$rel"
    done < <(find "$EN_ROOT/$subdir" -type f -name '*.adoc' -print0)
}

check_tree pages
check_tree partials

if [[ $status -eq 0 ]]; then
    echo "OK: no untranslated lines detected."
fi

exit $status