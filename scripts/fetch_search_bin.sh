#!/bin/sh
# Download the prebuilt signalpost-search binary for this platform and verify it against SHA256SUMS.
set -eu
TAG="${SEARCH_BIN_TAG:-search-rs-v0.2.0}"
BASE="https://github.com/tbd-hackathons/signalpost-agent/releases/download/$TAG"
case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) TARGET=x86_64-unknown-linux-musl ;;
  Linux-aarch64|Linux-arm64) TARGET=aarch64-unknown-linux-musl ;;
  Darwin-arm64) TARGET=aarch64-apple-darwin ;;
  *) echo "no prebuilt search binary for $(uname -s)-$(uname -m)"; exit 1 ;;
esac
NAME="signalpost-search-$TARGET"
TMP="$(mktemp -d)"
curl -fsSL "$BASE/$NAME" -o "$TMP/$NAME"
curl -fsSL "$BASE/SHA256SUMS" -o "$TMP/SHA256SUMS"
(cd "$TMP" && grep " $NAME\$" SHA256SUMS | shasum -a 256 -c -)
mkdir -p bin
install -m 0755 "$TMP/$NAME" bin/signalpost-search
rm -rf "$TMP"
echo "installed bin/signalpost-search ($TARGET, $TAG)"
