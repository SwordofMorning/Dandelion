#!/usr/bin/env python3
# -*- coding: utf-8 -*-
##
 # @file ascii_check.py
 # @brief Report non-ASCII characters in source files.
 #
 # Scans *.py files under the given directories (or explicit files) and
 # prints every non-ASCII character in clang-format style:
 #     <path>:<line>:<col>: non-ASCII '<char>' (U+XXXX)
 #
 # Exit code: 0 = clean, 1 = findings.
 #
 # Usage:
 #     python3 test/ascii_check.py src/
 #     python3 test/ascii_check.py src/ mk/ main.py
 #

import argparse
import os
import sys

SKIP_DIRS = {
    ".git", "__pycache__", ".env", ".log", "llm",
    "build", "dist", "node_modules", "venv", ".venv",
}


def iter_py_files(root):
    """Yield *.py files under root, skipping noise directories."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def line_col_from_offset(raw, offset):
    """Map a byte offset to 1-based (line, byte-column)."""
    line = raw.count(b"\n", 0, offset) + 1
    last_nl = raw.rfind(b"\n", 0, offset)
    return line, offset - last_nl


def check_file(path):
    """Return a list of (line, col, char, unicode_hex_or_None) findings."""
    with open(path, "rb") as f:
        raw = f.read()

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        line, col = line_col_from_offset(raw, e.start)
        return [(line, col, "<invalid UTF-8 byte>", None)]

    findings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for col, ch in enumerate(line, 1):
            if ord(ch) > 127:
                findings.append((lineno, col, ch, "U+%04X" % ord(ch)))
    return findings


def main():
    ap = argparse.ArgumentParser(
        description="Report non-ASCII characters in source files.")
    ap.add_argument("paths", nargs="*", default=["src"],
                    help="Files or directories (directories: *.py only).")
    args = ap.parse_args()

    files = []
    for p in args.paths:
        if os.path.isdir(p):
            files.extend(iter_py_files(p))
        else:
            files.append(p)
    files = sorted(set(files))

    total = 0
    for fp in files:
        for lineno, col, ch, code in check_file(fp):
            label = ("'%s' (%s)" % (ch, code)) if code else ch
            print("%s:%d:%d: non-ASCII %s" % (fp, lineno, col, label))
            total += 1

    print("---")
    print("%d non-ASCII occurrence(s) in %d file(s)." % (total, len(files)))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
