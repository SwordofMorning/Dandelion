#!/bin/bash
##
# @file .github/scripts/smoke-test.sh
# @brief Smoke-test the FINAL Linux release archive inside the target distro.
#
# Usage: smoke-test.sh <archive.tar.gz> <ubuntu-release e.g. 18.04> <expected version e.g. v0.1.0>
#
# The archive is mounted read-only into a clean ubuntu:<release> container
# (no repo checkout, no system Python, no network, no API key) and unpacked
# by an unprivileged uid into /tmp. Then, as that user:
#   1. dandelion.sh and bin/dandelion must be mode 0755
#   2. ldd on bin/dandelion must resolve every library (GLIBC/ABI check)
#   3. ./dandelion.sh --version must print exactly <expected version>, rc 0
#   4. ./dandelion.sh --self-test must print "Self-test OK", rc 0
#   5. legacy startup (no args, stdin=/dev/null) must reach config loading
# Every command has a timeout; any failure exits non-zero.
#
# @note The inner script is passed with `bash -c` (NOT via stdin): docker run
#   without -i does not forward stdin, which would silently turn a `bash -s`
#   heredoc into a no-op that "passes".
#
set -euo pipefail

ARCHIVE="${1:?archive path}"
DISTRO="${2:?ubuntu release}"
EXPECTED="${3:?expected version}"

ARCHIVE_DIR="$(cd "$(dirname "$ARCHIVE")" && pwd)"
ARCHIVE_NAME="$(basename "$ARCHIVE")"

echo "[*] Smoke test: $ARCHIVE_NAME on ubuntu:$DISTRO (expect $EXPECTED)"
sha256sum "$ARCHIVE"

INNER=$(cat <<'INNER_EOF'
set -euo pipefail
ARCHIVE="$1"
EXPECTED="$2"
echo "[*] Container: $(. /etc/os-release && echo "$PRETTY_NAME"), uid=$(id -u), $(ldd --version | head -1)"
if command -v python3 >/dev/null 2>&1; then
  echo "[!] note: python3 exists in this image ($(command -v python3)); the artifact must not use it"
fi

mkdir -p /tmp/smoke
tar -xzf "$ARCHIVE" -C /tmp/smoke
cd /tmp/smoke/dandelion
ls -la

echo "[*] 1. Permissions"
for f in dandelion.sh bin/dandelion; do
  mode="$(stat -c '%a' "$f")"
  echo "    $f -> $mode"
  [ "$mode" = "755" ] || { echo "[-] $f has mode $mode (expected 755)"; exit 1; }
done

echo "[*] 2. Native closure (ldd)"
missing="$(ldd bin/dandelion 2>&1 | grep -E 'not found' || true)"
if [ -n "$missing" ]; then
  echo "[-] bin/dandelion has unresolved dependencies:"
  echo "$missing"
  exit 1
fi
warn=0
while IFS= read -r -d '' so; do
  out="$(ldd "$so" 2>&1 | grep -E 'not found' || true)"
  if [ -n "$out" ]; then
    warn=$((warn + 1))
    echo "    [!] $so: $out"
  fi
done < <(find bin -type f \( -name '*.so' -o -name '*.so.*' \) -print0)
echo "    main executable OK; extension modules with unresolved libs: $warn (checked functionally by --self-test)"

run() {
  # Clean environment: no inherited PYTHON*/LD_* variables.
  local t="$1"; shift
  env -i PATH=/usr/bin:/bin HOME=/tmp LC_ALL=C PYTHONFAULTHANDLER=1 timeout "$t" "$@" < /dev/null
}

echo "[*] 3. --version"
set +e
out="$(run 60 ./dandelion.sh --version 2>&1)"; rc=$?
set -e
echo "$out"
[ "$rc" -eq 0 ] || { echo "[-] --version exited $rc"; exit 1; }
[ "$out" = "$EXPECTED" ] || { echo "[-] --version printed '$out', expected '$EXPECTED'"; exit 1; }

echo "[*] 4. --self-test"
set +e
out="$(run 180 ./dandelion.sh --self-test 2>&1)"; rc=$?
set -e
echo "$out"
[ "$rc" -eq 0 ] || { echo "[-] --self-test exited $rc"; exit 1; }
echo "$out" | grep -q "^Self-test OK$" || { echo "[-] --self-test did not report OK"; exit 1; }

echo "[*] 5. Legacy startup (no args)"
set +e
out="$(run 60 ./dandelion.sh 2>&1)"; rc=$?
set -e
echo "$out" | head -5
echo "$out" | grep -q "Initializing Dandelion Project" || { echo "[-] entry did not start (rc=$rc)"; echo "$out"; exit 1; }
echo "$out" | grep -qE "Agent Initialization Successful|Failed to load config" || { echo "[-] unexpected startup output (rc=$rc)"; echo "$out"; exit 1; }

echo "SMOKE-INNER-COMPLETE"
INNER_EOF
)

set +e
LOG="$(timeout 600 docker run --rm \
  --network none \
  --user 1001:1001 \
  -e HOME=/tmp \
  -e LC_ALL=C \
  -v "$ARCHIVE_DIR:/dist:ro" \
  "ubuntu:$DISTRO" \
  /bin/bash -c "$INNER" smoke "/dist/$ARCHIVE_NAME" "$EXPECTED" < /dev/null 2>&1)"
RC=$?
set -e
echo "$LOG"
[ "$RC" -eq 0 ] || { echo "[-] Smoke FAILED on ubuntu:$DISTRO (rc=$RC)"; exit 1; }
# Belt and braces: the inner script must have run to its last line.
echo "$LOG" | grep -q "^SMOKE-INNER-COMPLETE$" || { echo "[-] Smoke script did not run to completion"; exit 1; }
echo "[+] Smoke OK: $ARCHIVE_NAME on ubuntu:$DISTRO"
