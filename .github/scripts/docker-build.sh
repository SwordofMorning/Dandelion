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

echo "[*] Install uv (installer auto-picks a glibc/musl build for old distros)"
curl -LsSf https://astral.sh/uv/install.sh | sh

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
