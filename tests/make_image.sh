#!/usr/bin/env bash
# Builds a small NTFS image with a known set of deleted files for the test suite.
# Needs ntfs-3g (mkntfs + the FUSE driver). Run as root or with FUSE access.
#
#   tests/make_image.sh /tmp/ntfs-test
#
# Produces <dir>/test.img and <dir>/manifest.json (path -> sha256, size, deleted?).
set -euo pipefail

OUT="${1:?usage: make_image.sh <output dir>}"
SIZE_MB="${SIZE_MB:-64}"
mkdir -p "$OUT"
IMG="$OUT/test.img"
MNT="$OUT/mnt"

command -v mkntfs >/dev/null || { echo "mkntfs not found (apt install ntfs-3g)"; exit 1; }
command -v ntfs-3g >/dev/null || { echo "ntfs-3g not found"; exit 1; }

rm -f "$IMG"
dd if=/dev/zero of="$IMG" bs=1M count="$SIZE_MB" status=none
mkntfs -F -q -c 4096 "$IMG" >/dev/null 2>&1
mkdir -p "$MNT"
ntfs-3g "$IMG" "$MNT"
trap 'fusermount -u "$MNT" 2>/dev/null || umount "$MNT" 2>/dev/null || true' EXIT

python3 "$(dirname "$0")/populate.py" "$MNT" "$OUT/manifest.json"
sync
fusermount -u "$MNT" || umount "$MNT"
trap - EXIT
echo "image ready: $IMG"
