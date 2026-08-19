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
#
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
export UV_PYTHON_INSTALL_DIR=/work/.uv-python
export PATH="/root/.local/bin:/work/venv/bin:$PATH"

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

echo "[*] Install build toolchain (gcc for Nuitka, curl for uv)"
apt-get install -y -qq --no-install-recommends \
  curl ca-certificates gcc g++ make >/dev/null

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

if [ ! -d /work/venv ]; then
  uv venv /work/venv --python 3.11
fi

echo "[*] Install project dependencies (editable, incl. dev extras)"
uv pip install --python /work/venv/bin/python -e ".[dev]"

echo "[*] Install patchelf from PyPI (apt 0.9 lacks --force-rpath)"
if ! uv pip install --python /work/venv/bin/python patchelf >/dev/null 2>&1; then
  echo "[!] PyPI patchelf unavailable here; falling back to apt patchelf"
  apt-get install -y -qq patchelf >/dev/null 2>&1 || true
fi

echo "[*] Build with Nuitka (mk/make.py all)"
cd /work
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
    fi
    python mk/make.py all
  else
    exit 1
  fi
fi

echo "[*] Restore host ownership of build outputs (host runner is non-root)"
chown -R "$(stat -c '%u:%g' /work)" \
  /work/venv /work/.uv-python /work/.nuitka-cache /work/build 2>/dev/null || true

echo "[+] Docker build OK (ubuntu ${DANDELION_TARGET:-unknown})"
