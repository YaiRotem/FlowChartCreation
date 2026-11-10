#!/usr/bin/env sh
# FlowChartCreation - recalculate CHECKSUMS.sha256 after you edited a file on purpose
# (for example the ABOUT block at the top of FlowChartCreation.html).
cd "$(dirname "$0")" || exit 1
printf 'This records the CURRENT files as the correct ones. Continue? (y/N) '
read -r ok
[ "$ok" = "y" ] || [ "$ok" = "Y" ] || exit 1
if command -v sha256sum >/dev/null 2>&1; then H="sha256sum"; else H="shasum -a 256"; fi
{ find . -maxdepth 1 -type f; [ -d docs ] && find ./docs -maxdepth 1 -type f; } | sed 's|^\./||' | grep -v -e '^CHECKSUMS.sha256$' -e '\.bak$' | LC_ALL=C sort | while read -r f; do $H "$f"; done > "${TMPDIR:-/tmp}/fcc_checksums.$$" && mv "${TMPDIR:-/tmp}/fcc_checksums.$$" CHECKSUMS.sha256
echo "Updated CHECKSUMS.sha256 ($(wc -l < CHECKSUMS.sha256) files). Now run ./verify_files.sh"
