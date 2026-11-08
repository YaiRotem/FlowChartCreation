#!/usr/bin/env sh
# FlowChartCreation - verify that the files are intact (SHA-256 against CHECKSUMS.sha256)
cd "$(dirname "$0")" || exit 1
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum -c CHECKSUMS.sha256
else
  shasum -a 256 -c CHECKSUMS.sha256
fi
