#!/bin/bash
##
# @file mk/scripts/docker_build.sh
# @date 2026/11/28
# @brief Build Dandelion Nuitka standalone inside manylinux2014 (glibc 2.17, devtoolset-10 GCC 10).
#
# Runs as root inside quay.io/pypa/manylinux2014_{x86_64,aarch64}; the repo is
# mounted at /work and prebuilt dandelion-core wheels are at /work/dist-wheels.
#
# Why manylinux2014 + prebuilt dandelion-core wheel:
#   - Building Nuitka inside manylinux2014 (glibc 2.17 + devtoolset-10 GCC 10)
#     produces ONE single Linux binary per architecture that runs on Ubuntu
#     18.04-26.04 AND CentOS 8.2 / AlmaLinux 8+.
#   - Python 3.11 comes from python-build-standalone (astral/uv), which ships a
#     glibc 2.17 baseline AND shared libpython3.11.so (--static-libpython=no).
#   - dandelion-core is installed strictly from the prebuilt cp311 wheel with
#     --reinstall-package dandelion-core so cached venvs never keep stale code.
#   - Installs `-e ".[dev]"` WITHOUT `--no-deps` so root-only dependencies
#     such as prompt-toolkit are preserved in the standalone artifact.
#
set -euo pipefail

export UV_PYTHON_INSTALL_DIR=/work/.uv-python
export PATH="/usr/local/bin:/root/.local/bin:/work/venv/bin:$PATH"

restore_ownership() {
  local owner
  owner="$(stat -c '%u:%g' /work)"
  chown -R "$owner" \
    /work/venv /work/.uv-python /work/.nuitka-cache /work/build \
    /work/mk/lib/build_info.py /work/*.egg-info \
    2>/dev/null || true
}
trap restore_ownership EXIT

echo "[*] Toolchain check inside manylinux2014 ($(uname -m))"
gcc --version | head -1
ldd --version | head -1

if ! command -v ccache >/dev/null 2>&1; then
  echo "[*] Installing ccache (best-effort)"
  yum install -y -q ccache >/dev/null 2>&1 || true
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "[*] Installing uv (pinned release, sha256-verified, no pipe-to-sh)"
  UV_VERSION="0.13.10"
  UV_ARCH="$(uname -m)"
  case "$UV_ARCH" in
    x86_64)
      # x86_64 gnu builds have a manylinux_2_17 baseline (glibc 2.17+).
      UV_TARGET="x86_64-unknown-linux-gnu"
      ;;
    aarch64)
      # aarch64 gnu builds require glibc 2.28+; manylinux2014 has glibc 2.17,
      # so use the static musl build.
      UV_TARGET="aarch64-unknown-linux-musl"
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
fi

echo "[*] Install Python 3.11 (python-build-standalone, glibc 2.17 baseline + shared libpython)"
uv python install 3.11

if [ ! -x /work/venv/bin/python ]; then
  rm -rf /work/venv
  uv venv /work/venv --python 3.11
fi

echo "[*] Install prebuilt dandelion-core cp311 wheel + root deps & dev extras (preserving prompt-toolkit)"
shopt -s nullglob
wheels=(/work/dist-wheels/dandelion_core-*-cp311-*.whl)
if [ "${#wheels[@]}" -ne 1 ]; then
  echo "[-] Error: expected exactly 1 prebuilt cp311 wheel in /work/dist-wheels, found ${#wheels[@]}"
  ls -la /work/dist-wheels 2>/dev/null || true
  exit 1
fi
WHEEL_FILE="${wheels[0]}"
EXPECTED_WHEEL_SHA256="$(sha256sum "$WHEEL_FILE" | awk '{print $1}')"
echo "[+] Using prebuilt wheel: $WHEEL_FILE"
uv pip install --python /work/venv/bin/python \
  --reinstall-package dandelion-core \
  "$WHEEL_FILE" \
  -e ".[dev]"
uv pip install --python /work/venv/bin/python \
  --no-deps --reinstall \
  "$WHEEL_FILE"

# Sanity check before Nuitka: verify prompt_toolkit, _native, and strict local wheel provenance.
EXPECTED_WHEEL_SHA256="$EXPECTED_WHEEL_SHA256" /work/venv/bin/python -c '
import importlib.metadata as m, json, os, prompt_toolkit, dandelion_core
from dandelion_core.utils.llm_provider import _native
raw = m.distribution("dandelion-core").read_text("direct_url.json")
assert raw, "Missing PEP 610 direct_url.json (non-local dandelion-core)"
du = json.loads(raw)
url = du.get("url", "")
assert url.startswith("file:///work/dist-wheels/"), f"Non-local dandelion-core: {du}"
exp = os.environ.get("EXPECTED_WHEEL_SHA256", "")
act = du.get("archive_info", {}).get("hashes", {}).get("sha256", "")
if exp and act:
    assert act == exp, f"Wheel SHA256 mismatch: {act} != {exp}"
print("[+] Verified local wheel env:", _native.__file__, url)
'

if ! command -v patchelf >/dev/null 2>&1; then
  echo "[*] Installing patchelf from PyPI"
  uv pip install --python /work/venv/bin/python patchelf >/dev/null 2>&1 || true
fi

export CCACHE_DIR=/work/.nuitka-cache/ccache
export NUITKA_CACHE_DIR_CCACHE="$CCACHE_DIR"
export CCACHE_MAXSIZE="${CCACHE_MAXSIZE:-1.5G}"
export CCACHE_COMPILERCHECK=content
export CCACHE_BASEDIR=/work
mkdir -p "$CCACHE_DIR"

echo "[*] Build with Nuitka (mk/make.py all)"
cd /work
/work/venv/bin/python mk/make.py all

echo "[+] Docker build OK (manylinux2014 $(uname -m))"
