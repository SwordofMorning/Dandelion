#!/usr/bin/env python3
# -*- coding: utf-8 -*-
##
 # @file mk/lib/ascii_check.py
 # @date 2026/11/28
 #
 # @brief Report non-ASCII characters in Python and C/C++ source files.
 #
 # Scans *.py, *.cpp, *.hpp, and *.h files under the given directories (or
 # explicit files) and prints every non-ASCII character in clang-format style:
 #     <path>:<line>:<col>: non-ASCII '<char>' (U+XXXX)
 #
 # Exit code: 0 = clean, 1 = findings.
 #
 # Usage:
 #     python3 mk/make.py ascii
 #     python3 mk/lib/ascii_check.py src/ packages/dandelion-core/src/ mk/ main.py
 #

import argparse
import os
import sys

ALLOWED_EXTS = (".py", ".cpp", ".hpp", ".h")

DEFAULT_PATHS = (
    "src",
    "packages/dandelion-core/src",
    "mk",
    "main.py",
)

SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".env",
    ".log",
    ".data",
    ".playground",
    ".nuitka-cache",
    ".uv-python",
    ".wheel-deps-cache",
    "llm",
    "build",
    "dist",
    "dist-wheels",
    "dist-sdist",
    "upload_stage",
    "node_modules",
    "venv",
    ".venv",
}

SKIP_FILES = set()


##
 # @brief Yield source files matching ALLOWED_EXTS under root.
 #
 # @param root Directory path to traverse recursively.
 #
 # @return Generator yielding matching file paths.
 #
def iter_source_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in SKIP_DIRS and not d.endswith(".egg-info")
        )
        for fn in sorted(filenames):
            if fn.endswith(ALLOWED_EXTS) and fn not in SKIP_FILES:
                yield os.path.join(dirpath, fn)
            # End-if
        # End-for
    # End-for
# End-def


##
 # @brief Map a byte offset to 1-based (line, byte-column).
 #
 # @param raw Raw file bytes.
 # @param offset Byte index into raw.
 #
 # @return (line, col) 1-based integer tuple.
 #
def line_col_from_offset(raw, offset):
    line = raw.count(b"\n", 0, offset) + 1
    last_nl = raw.rfind(b"\n", 0, offset)
    return line, offset - last_nl
# End-def


##
 # @brief Scan a single file and return all non-ASCII findings.
 #
 # @param path Path to the file to check.
 #
 # @return List of (line, col, char, unicode_hex_or_None) tuples.
 #
def check_file(path):
    with open(path, "rb") as f:
        raw = f.read()
    # End-with

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        line, col = line_col_from_offset(raw, exc.start)
        return [(line, col, "<invalid UTF-8 byte>", None)]
    # End-try

    findings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for col, ch in enumerate(line, 1):
            if ord(ch) > 127:
                findings.append((lineno, col, ch, "U+%04X" % ord(ch)))
            # End-if
        # End-for
    # End-for
    return findings
# End-def


##
 # @brief Check all source files under the given paths and print clang-style diagnostics.
 #
 # @param paths Iterable of file or directory paths to scan.
 # @param base_dir Optional base directory used to resolve relative paths and format output.
 #
 # @return Integer exit code (0 = clean, 1 = findings or missing path).
 #
def check_paths(paths, base_dir=None):
    files = []
    missing = []
    for raw_path in paths:
        target = raw_path
        if base_dir and not os.path.isabs(target):
            target = os.path.join(base_dir, target)
        # End-if
        if os.path.isdir(target):
            files.extend(iter_source_files(target))
        elif os.path.isfile(target):
            if os.path.basename(target) not in SKIP_FILES:
                files.append(target)
            # End-if
        else:
            missing.append(raw_path)
        # End-if
    # End-for

    if missing:
        for m in missing:
            print("[-] Path not found: %s" % m)
        # End-for
        return 1
    # End-if

    files = sorted(set(files))
    total = 0
    for fp in files:
        display = os.path.relpath(fp, base_dir) if base_dir else fp
        for lineno, col, ch, code in check_file(fp):
            label = ("'%s' (%s)" % (ch, code)) if code else ch
            print("%s:%d:%d: non-ASCII %s" % (display, lineno, col, label))
            total += 1
        # End-for
    # End-for

    print("---")
    print("%d non-ASCII occurrence(s) in %d file(s)." % (total, len(files)))
    return 1 if total else 0
# End-def


##
 # @brief Command-line entry point for ASCII-only source verification.
 #
 # @param argv Optional list of CLI arguments; None reads sys.argv[1:].
 #
 # @return Integer exit code (0 = clean, 1 = findings).
 #
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Report non-ASCII characters in Python and C/C++ source files."
    )
    ap.add_argument(
        "paths",
        nargs="*",
        default=list(DEFAULT_PATHS),
        help="Files or directories to scan (*.py, *.cpp, *.hpp, *.h).",
    )
    args = ap.parse_args(argv)
    return check_paths(args.paths)
# End-def


if __name__ == "__main__":
    sys.exit(main())
# End-if
