#!/bin/bash
##
# @file .github/scripts/docker-build.sh
# @brief Build Dandelion with Nuitka inside a target-distro Ubuntu container.
#
# Runs as root inside the container; the repo is mounted at /work.
# Designed to work on EOL images (ubuntu:18.04, ubuntu:20.04) and current
# ones (22.04/24.04/26.04):
#   - EOL apt repos fall back to old-releases.ubuntu.com automatically
#     (ports.ubuntu.com -> old-releases.ports.ubuntu.com on arm64 images).
#   - Python 3.11 comes from python-build-standalone (astral/uv), whose
#     binaries have a glibc 2.17 baseline and thus run on every target here.
#   - uv itself is installed from a pinned GitHub release tarball verified
#     against the official .sha256 file (no curl|sh pipeline).
#   - patchelf comes from PyPI (apt's 0.9 lacks --force-rpath).
#   - ccache caches the C objects Nuitka compiles. CCACHE_DIR and Nuitka's
#     own ccache dir are pinned to the SAME path inside .nuitka-cache, which
#     is the directory the workflow saves/restores with actions/cache.
#   - The venv is rebuilt from scratch whenever its fingerprint (pyproject,
#     Python, uv) changes, so stale packages (e.g. pandas) can never leak in.
#   - ELF strip runs in mk/make.py (inside this container, i.e. with the
#     target distro's binutils) and is controlled by DANDELION_STRIP.
#
# @env DANDELION_VERSION  version/tag (passed through to mk/make.py)
# @env DANDELION_TARGET   distro label, e.g. "18.04"
# @env DANDELION_STRIP    "1" (default) / "0"
# @env CCACHE_MAXSIZE     ccache size cap (default 1.5G)
#
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
export LC_ALL=C
export UV_PYTHON_INSTALL_DIR=/work/.uv-python
export PATH="/root/.local/bin:/work/venv/bin:$PATH"

# Always hand outputs back to the (non-root) host runner user, even when the
# build fails; otherwise the cache save step cannot read root-owned files.
restore_ownership() {
  local owner
  owner="$(stat -c '%u:%g' /work)"
  chown -R "$owner" /work/venv /work/.uv-python /work/.nuitka-cache /work/build \
    2>/dev/null || true
}
trap restore_ownership EXIT

echo "[*] apt-get update (retries with old-releases for EOL distros)"
if ! apt-get update -qq; then
  # bionic (18.04) and focal (20.04) are EOL: their repos moved to
  # old-releases.ubuntu.com. arm64 images use ports.ubuntu.com instead of
  # archive/security.ubuntu.com, so cover both hostname families.
  # The sed is harmless for supported distros because this branch only
  # runs after a failed update.
  sed -i \
    's|http://archive.ubuntu.com|http://old-releases.ubuntu.com|g; s|http://security.ubuntu.com|http://old-releases.ubuntu.com|g; s|https://archive.ubuntu.com|https://old-releases.ubuntu.com|g; s|https://security.ubuntu.com|https://old-releases.ubuntu.com|g; s|http://ports.ubuntu.com|http://old-releases.ports.ubuntu.com|g; s|https://ports.ubuntu.com|https://old-releases.ports.ubuntu.com|g' \
    /etc/apt/sources.list
  apt-get update -qq
fi

echo "[*] Install build toolchain (gcc/ccache for Nuitka, binutils for strip, curl for uv)"
apt-get install -y -qq --no-install-recommends \
  curl ca-certificates gcc g++ make ccache binutils >/dev/null

echo "[*] Install uv (pinned release, sha256-verified, no pipe-to-sh)"
UV_VERSION="0.11.16"
UV_ARCH="$(uname -m)"
case "$UV_ARCH" in
  x86_64)
    # x86_64 gnu builds have a manylinux_2_17 baseline (glibc 2.17+);
    # every matrix target (18.04+ = glibc 2.27+) is covered.
    UV_TARGET="x86_64-unknown-linux-gnu"
    ;;
  aarch64)
    # aarch64 gnu builds need glibc 2.28+; Ubuntu 18.04 has glibc 2.27,
    # so fall back to the fully static musl build on older distros.
    UV_GLIBC="$(ldd --version 2>/dev/null | sed -n '1s/.*\([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p' || true)"
    if awk "BEGIN{exit !(${UV_GLIBC:-0} < 2.28)}"; then
      UV_TARGET="aarch64-unknown-linux-musl"
    else
      UV_TARGET="aarch64-unknown-linux-gnu"
    fi
    ;;
  *)
    echo "[-] Unsupported architecture: $UV_ARCH"
    exit 1
    ;;
esac

UV_URL="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${UV_TARGET}.tar.gz"
curl -fsSL --max-time 300 -o /tmp/uv.tar.gz "$UV_URL"
curl -fsSL --max-time 60 -o /tmp/uv.tar.gz.sha256 "${UV_URL}.sha256"
EXPECTED="$(awk '{print $1}' /tmp/uv.tar.gz.sha256)"
ACTUAL="$(sha256sum /tmp/uv.tar.gz | awk '{print $1}')"
if [ "$EXPECTED" != "$ACTUAL" ]; then
  echo "[-] uv checksum mismatch: expected $EXPECTED, got $ACTUAL"
  exit 1
fi

# Extract layout-agnostically (tarballs may or may not have a top-level
# dir) and install the uv/uvx binaries into /usr/local/bin.
rm -rf /tmp/uv-extract
mkdir -p /tmp/uv-extract
tar -xzf /tmp/uv.tar.gz -C /tmp/uv-extract
UV_BIN="$(find /tmp/uv-extract -type f -name uv | head -1)"
UVX_BIN="$(find /tmp/uv-extract -type f -name uvx | head -1)"
if [ -z "$UV_BIN" ] || [ -z "$UVX_BIN" ]; then
  echo "[-] uv tarball layout unexpected (uv/uvx binaries not found)"
  exit 1
fi
install -m 755 "$UV_BIN" /usr/local/bin/uv
install -m 755 "$UVX_BIN" /usr/local/bin/uvx
echo "[+] uv $UV_VERSION ($UV_TARGET) installed and verified"

echo "[*] Install Python 3.11 (python-build-standalone, glibc 2.17 baseline)"
uv python install 3.11

# ----- Clean venv guarantee -----
# The venv is restored from the GHA cache. Rebuild it from scratch whenever
# its fingerprint differs, so packages removed from pyproject.toml (pandas,
# numpy) cannot survive in a stale venv and be bundled again.
VENV_FP="$( { sha256sum /work/pyproject.toml | awk '{print $1}'; \
              uv python find 3.11; echo "uv=$UV_VERSION"; } | sha256sum | awk '{print $1}')"
if [ -f /work/venv/.dandelion-fingerprint ] && \
   [ "$(cat /work/venv/.dandelion-fingerprint)" = "$VENV_FP" ] && \
   [ -x /work/venv/bin/python ]; then
  echo "[+] Reusing cached venv (fingerprint $VENV_FP)"
else
  echo "[*] Creating a clean venv (fingerprint $VENV_FP)"
  rm -rf /work/venv
  uv venv /work/venv --python 3.11
fi

echo "[*] Install project dependencies (editable, incl. dev extras)"
uv pip install --python /work/venv/bin/python -e ".[dev]"

echo "[*] Install patchelf from PyPI (apt 0.9 lacks --force-rpath)"
if ! uv pip install --python /work/venv/bin/python patchelf >/dev/null 2>&1; then
  echo "[!] PyPI patchelf unavailable here; falling back to apt patchelf"
  apt-get install -y -qq patchelf >/dev/null 2>&1 || true
fi
echo "$VENV_FP" > /work/venv/.dandelion-fingerprint

echo "[*] Verify removed heavy packages are not installed in the build venv"
if /work/venv/bin/python -c "import importlib.util, sys; sys.exit(0 if any(importlib.util.find_spec(m) for m in ('pandas', 'numpy')) else 1)"; then
  echo "[-] pandas/numpy present in the build venv; something still depends on them:"
  uv pip tree --python /work/venv/bin/python --invert --package numpy 2>/dev/null || true
  exit 1
fi
echo "[+] pandas/numpy absent from the build venv"

# ----- ccache (C object cache for Nuitka) -----
# Single source of truth for the object-cache location: the workflow caches
# /work/.nuitka-cache, so ccache MUST live inside it.
export CCACHE_DIR=/work/.nuitka-cache/ccache
export NUITKA_CACHE_DIR_CCACHE="$CCACHE_DIR"
export NUITKA_CCACHE_BINARY="$(command -v ccache)"
export CCACHE_MAXSIZE="${CCACHE_MAXSIZE:-1.5G}"
# Compare compilers by content: the gcc-9 fallback below or an image update
# then never reuses objects produced by a different compiler binary.
export CCACHE_COMPILERCHECK=content
export CCACHE_BASEDIR=/work
mkdir -p "$CCACHE_DIR"

ccache_activity() {
  # Version-agnostic: ccache 3.x prints "cache hit (direct)  12" / "cache
  # miss  34", 4.x prints "Hits: 12 / 46" / "Misses: 34". Any non-zero
  # hit/miss counter proves Nuitka actually routed compiles through ccache.
  ccache -s 2>/dev/null | grep -Ei 'hit|miss' | grep -Eq '[1-9]'
}

mkdir -p /work/build
TOOLCHAIN=/work/build/toolchain.txt
{
  echo "target=ubuntu${DANDELION_TARGET:-unknown} arch=$(uname -m)"
  echo "image_os=$(. /etc/os-release && echo "$PRETTY_NAME")"
  echo "glibc=$(ldd --version 2>/dev/null | head -1)"
  echo "gcc=$(gcc -dumpfullversion 2>/dev/null || gcc -dumpversion)"
  echo "binutils=$(strip --version | head -1)"
  echo "ccache=$(ccache --version | head -1)"
  echo "uv=$UV_VERSION ($UV_TARGET)"
  echo "python=$(/work/venv/bin/python -c 'import sys; print(sys.version.split()[0])')"
  echo "nuitka=$(/work/venv/bin/python -m nuitka --version 2>/dev/null | head -1)"
  echo "venv_fingerprint=$VENV_FP"
} > "$TOOLCHAIN"
echo "=== Toolchain ==="
cat "$TOOLCHAIN"

echo "=== ccache (restored cache, stats zeroed for this run) ==="
# -z resets the counters only; cached objects are kept.
ccache -z >/dev/null
ccache -s || true

echo "[*] Build with Nuitka (mk/make.py all)"
cd /work
BUILD_START=$(date +%s)
if ! python mk/make.py all; then
  # bionic ships gcc 7.5. If the installed Nuitka version requires a newer
  # compiler, upgrade gcc via the ubuntu-toolchain-r PPA and retry once.
  # Only attempted when the first build failed AND the default gcc is old,
  # so modern distros fail fast with the original error.
  GCC_MAJOR=$(gcc -dumpfullversion 2>/dev/null | cut -d. -f1 || echo 0)
  if [ "$GCC_MAJOR" -lt 9 ]; then
    echo "[!] Nuitka build failed; retrying once with a newer gcc (PPA)"
    apt-get install -y -qq software-properties-common >/dev/null 2>&1 || true
    add-apt-repository -y ppa:ubuntu-toolchain-r/test >/dev/null 2>&1 || true
    apt-get update -qq >/dev/null 2>&1 || true
    apt-get install -y -qq gcc-9 g++-9 >/dev/null 2>&1 || true
    if command -v gcc-9 >/dev/null 2>&1; then
      export CC=gcc-9 CXX=g++-9
      # Record the toolchain switch: this run is not comparable with gcc-7 runs.
      echo "gcc_fallback=$(gcc-9 -dumpfullversion 2>/dev/null)" >> "$TOOLCHAIN"
    fi
    python mk/make.py all
  else
    exit 1
  fi
fi
BUILD_END=$(date +%s)

echo "=== ccache (this run) ==="
ccache -s || true
echo "ccache dir size: $(du -sh "$CCACHE_DIR" 2>/dev/null | cut -f1) (max $CCACHE_MAXSIZE)"
if ccache_activity; then
  echo "[+] ccache was used by this build"
  echo "ccache_used=yes" >> "$TOOLCHAIN"
else
  echo "[!] ccache recorded NO compiler calls: the C object cache is INACTIVE for this build (no-cache fallback)"
  echo "ccache_used=no" >> "$TOOLCHAIN"
fi
echo "build_seconds=$((BUILD_END - BUILD_START))" >> "$TOOLCHAIN"

echo "[+] Docker build OK (ubuntu ${DANDELION_TARGET:-unknown}, $((BUILD_END - BUILD_START))s)"
