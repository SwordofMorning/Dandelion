#!/usr/bin/env bash
##
# @file mk/scripts/build_curl_static.sh
# @date 2026/11/28
# @brief Build cached static zlib 1.3.1 + OpenSSL 3.0.15 + minimal HTTP/HTTPS-only
#        libcurl 8.11.1 inside manylinux2014 (glibc 2.17, devtoolset-10 GCC 10).
#
# Output layout:
#   ${DANDELION_STATIC_PREFIX}/include/
#   ${DANDELION_STATIC_PREFIX}/lib/{libz.a,libssl.a,libcrypto.a,libcurl.a}
#
# Built with -fPIC -fvisibility=hidden and --without-ca-bundle --without-ca-path
# so no CentOS-specific CA path is baked into libcurl.a and CMake links with
# -Wl,--exclude-libs,ALL (zero symbol collision with CPython's _ssl module).
#
set -euo pipefail

PREFIX="${DANDELION_STATIC_PREFIX:-/opt/dandelion-deps}"
ZLIB_VER="1.3.1"
ZLIB_SHA256="9a93b2b7dfdac77ceba5a558a580e74667dd6fede4585b91eefb60f03b72df23"
OPENSSL_VER="3.0.15"
OPENSSL_SHA256="23c666d0edf20f14249b3d8f0368acaee9ab585b09e1de82107c66e1f3ec9533"
CURL_VER="8.11.1"
CURL_TAG="8_11_1"
CURL_SHA256="a889ac9dbba3644271bd9d1302b5c22a088893719b72be3487bc3d401e5c4e80"

STAMP="${PREFIX}/.built-zlib${ZLIB_VER}-ssl${OPENSSL_VER}-curl${CURL_VER}-v2-$(uname -m)"

if [ -f "$STAMP" ] \
    && [ -f "${PREFIX}/lib/libz.a" ] \
    && [ -f "${PREFIX}/lib/libssl.a" ] \
    && [ -f "${PREFIX}/lib/libcrypto.a" ] \
    && [ -f "${PREFIX}/lib/libcurl.a" ]; then
    echo "[+] Reusing cached static zlib ${ZLIB_VER} + OpenSSL ${OPENSSL_VER} + libcurl ${CURL_VER} at ${PREFIX}"
    exit 0
fi

# Perl modules required by OpenSSL 3.x Configure on CentOS 7 (manylinux2014).
echo "[*] Ensuring Perl build modules for OpenSSL 3.x on CentOS 7 (manylinux2014)"
if ! perl -MIPC::Cmd -MData::Dumper -MFindBin -MTime::Piece -e '1' 2>/dev/null; then
    yum install -y -q perl-IPC-Cmd perl-Data-Dumper perl-FindBin perl-Time-Piece
fi
if ! perl -MIPC::Cmd -MData::Dumper -MFindBin -MTime::Piece -e '1' 2>/dev/null; then
    echo "[-] FATAL: Required Perl modules (IPC::Cmd, Data::Dumper, FindBin, Time::Piece) are missing for OpenSSL 3.x Configure." >&2
    exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
NPROC="$(nproc 2>/dev/null || echo 2)"
mkdir -p "${PREFIX}"

verify_sha256() {
    local file="$1"
    local expected="$2"
    local actual
    actual="$(sha256sum "$file" | awk '{print $1}')"
    if [ "$actual" != "$expected" ]; then
        echo "[-] SHA-256 mismatch for ${file}: expected ${expected}, got ${actual}"
        exit 1
    fi
    echo "[+] Verified SHA-256 for $(basename "$file")"
}

# 1. Static zlib 1.3.1 (-fPIC -fvisibility=hidden)
echo "[*] Building static zlib ${ZLIB_VER}..."
cd "$WORK"
curl -fsSL --retry 3 --max-time 180 \
    "https://zlib.net/fossils/zlib-${ZLIB_VER}.tar.gz" -o zlib.tar.gz
verify_sha256 zlib.tar.gz "$ZLIB_SHA256"
tar -xzf zlib.tar.gz
cd "zlib-${ZLIB_VER}"
CFLAGS="-O2 -fPIC -fvisibility=hidden" ./configure --prefix="${PREFIX}" --static
make -j"${NPROC}"
make install

# 2. Static OpenSSL 3.0.15 (no-shared, no-dso, -fPIC -fvisibility=hidden, TLS 1.2 + 1.3)
#    Normalize lib64 -> lib so CMake paths are identical on x86_64 and aarch64.
echo "[*] Building static OpenSSL ${OPENSSL_VER}..."
cd "$WORK"
curl -fsSL --retry 3 --max-time 300 \
    "https://github.com/openssl/openssl/releases/download/openssl-${OPENSSL_VER}/openssl-${OPENSSL_VER}.tar.gz" \
    -o openssl.tar.gz
verify_sha256 openssl.tar.gz "$OPENSSL_SHA256"
tar -xzf openssl.tar.gz
cd "openssl-${OPENSSL_VER}"
./Configure no-shared no-dso no-tests no-docs no-apps -fPIC -fvisibility=hidden \
    --prefix="${PREFIX}" --libdir=lib --openssldir=/etc/ssl
make -j"${NPROC}"
make install_sw

# 3. Static minimal HTTP/HTTPS-only libcurl 8.11.1
#    --without-ca-bundle --without-ca-path avoids baking CentOS CA paths into libcurl.a.
echo "[*] Building static minimal HTTP/HTTPS libcurl ${CURL_VER}..."
cd "$WORK"
if ! curl -fsSL --retry 3 --max-time 300 \
    "https://github.com/curl/curl/releases/download/curl-${CURL_TAG}/curl-${CURL_VER}.tar.gz" \
    -o curl.tar.gz; then
    curl -fsSL --retry 3 --max-time 300 \
        "https://curl.se/download/curl-${CURL_VER}.tar.gz" \
        -o curl.tar.gz
fi
verify_sha256 curl.tar.gz "$CURL_SHA256"
tar -xzf curl.tar.gz
cd "curl-${CURL_VER}"
PKG_CONFIG_PATH="${PREFIX}/lib/pkgconfig" \
CPPFLAGS="-I${PREFIX}/include" \
LDFLAGS="-L${PREFIX}/lib" \
LIBS="-ldl -lpthread" \
CFLAGS="-O2 -fPIC -fvisibility=hidden" \
./configure \
    --prefix="${PREFIX}" \
    --libdir="${PREFIX}/lib" \
    --disable-shared --enable-static \
    --disable-curl \
    --with-openssl="${PREFIX}" \
    --with-zlib="${PREFIX}" \
    --disable-ldap --disable-ldaps --disable-ftp --disable-file \
    --disable-rtsp --disable-dict --disable-telnet --disable-tftp \
    --disable-pop3 --disable-imap --disable-smb --disable-smtp \
    --disable-gopher --disable-mqtt --disable-manual --disable-docs \
    --without-libidn2 --without-libpsl --without-librtmp --without-gssapi \
    --without-brotli --without-zstd --without-nghttp2 --without-libssh2 \
    --without-ca-bundle --without-ca-path
make -j"${NPROC}"
make install

touch "$STAMP"
echo "[+] Static zlib ${ZLIB_VER} + OpenSSL ${OPENSSL_VER} + libcurl ${CURL_VER} installed to ${PREFIX}"
