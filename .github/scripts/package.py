#!/usr/bin/env python3
##
 # @file .github/scripts/package.py
 # @date 2026/10/22
 #
 # @brief Release archive contract: pack, verify, verify-set (CI helper).
 #
 # One implementation for Linux (.tar.gz) and Windows (.zip) so both archives
 # share the same naming, top-level layout and checks:
 #
 #   archive name : dandelion-<target_id>-<version>.<tar.gz|zip>
 #   target_id    : <os>-<arch>[-ubuntu<NN>], e.g. linux-aarch64-ubuntu18, win-x86_64
 #   layout       : a single top-level "dandelion/" directory (= build/dandelion)
 #
 # Sub-commands:
 #   pack       --src build/dandelion --out-dir dist --target-id ID --version V --platform linux|windows
 #   verify     --archive FILE --platform linux|windows
 #   verify-set --dir DIR --version V --targets ID1,ID2,...
 #
 # Exit code 0 = OK, 1 = contract violation, 2 = usage error.
 #
 # @note Python's tarfile/zipfile are used instead of GNU tar / Compress-Archive:
 #   POSIX modes are stored in BOTH formats (zip external_attr), dot-files and
 #   empty directories are always included (Compress-Archive skips Hidden items),
 #   and member order is deterministic.
 #

import argparse
import os
import re
import stat
import sys
import tarfile
import zipfile

TOP = "dandelion"
SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
EXT = {"linux": "tar.gz", "windows": "zip"}

# Required entries relative to the top-level dir. "/" suffix = directory.
REQUIRED_COMMON = [
    "version.txt",
    "bin/MANIFEST.txt",
    ".env/",
    ".log/.keep",
    "llm/memory/.keep",
    "llm/skill/.keep",
]
REQUIRED = {
    "linux": REQUIRED_COMMON + ["dandelion.sh", "bin/dandelion"],
    "windows": REQUIRED_COMMON + ["dandelion.cmd", "bin/dandelion.exe"],
}
# Entries whose POSIX mode must be exactly 0755 (Linux only).
EXECUTABLE = {"linux": ["dandelion.sh", "bin/dandelion"], "windows": []}


class ContractError(Exception):
    pass
# End-class


##
 # @brief Canonical archive file name; validates both components.
 #
def archive_name(target_id, version, platform):
    for label, value in (("target_id", target_id), ("version", version)):
        if not SAFE.match(value or ""):
            raise ContractError("invalid %s %r (allowed: [A-Za-z0-9._+-])" % (label, value))
        # End-if
    # End-for
    return "dandelion-%s-%s.%s" % (target_id, version, EXT[platform])
# End-def


##
 # @brief Walk src deterministically: yields (relpath with '/', abspath, is_dir).
 #
 # Symlinks (to files OR directories) are yielded as non-directory entries and
 # never descended into, so a symlinked directory can never vanish silently.
 #
def _walk(src):
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        links = [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]
        dirnames[:] = sorted(d for d in dirnames if d not in links)
        rel_dir = os.path.relpath(dirpath, src).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        if rel_dir:
            yield rel_dir, dirpath, True
        # End-if
        for fn in sorted(filenames + links):
            yield (rel_dir + "/" + fn if rel_dir else fn), os.path.join(dirpath, fn), False
        # End-for
    # End-for
# End-def


##
 # @brief True if a symlink target (relative, '/'-separated) stays inside the tree.
 #
def _link_inside(member_rel, target):
    if not target or target.startswith("/") or re.match(r"^[A-Za-z]:", target):
        return False
    # End-if
    parts = member_rel.split("/")[:-1]
    for comp in target.replace("\\", "/").split("/"):
        if comp in ("", "."):
            continue
        elif comp == "..":
            if not parts:
                return False
            # End-if
            parts.pop()
        else:
            parts.append(comp)
        # End-if
    # End-for
    return True
# End-def


##
 # @brief Validate a member listing: {relpath: (is_dir, mode_or_None)}.
 #
 # @return list of problems (empty = OK).
 #
def check_members(members, platform):
    problems = []
    for name in members:
        if name.startswith("/") or ".." in name.split("/"):
            problems.append("unsafe member path: " + name)
        # End-if
    # End-for
    for req in REQUIRED[platform]:
        if req.endswith("/"):
            key = req.rstrip("/")
            has = key in members or any(m.startswith(req) for m in members)
            if not has:
                problems.append("missing directory: " + req)
            # End-if
        elif req not in members or members[req][0]:
            problems.append("missing file: " + req)
        # End-if
    # End-for
    for exe in EXECUTABLE[platform]:
        if exe in members:
            mode = members[exe][1]
            if mode is None or stat.S_IMODE(mode) != 0o755:
                problems.append("bad mode for %s: %s (expected 0755)"
                                % (exe, "none" if mode is None else oct(stat.S_IMODE(mode))))
            # End-if
        # End-if
    # End-for
    return problems
# End-def


##
 # @brief Read members of an archive as {relpath-under-top: (is_dir, mode)}.
 #
def read_members(path):
    members = {}
    outside = []
    if path.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as tf:
            for m in tf.getmembers():
                name = m.name.rstrip("/")
                if m.islnk():
                    outside.append("hard link member not allowed: " + name)
                elif m.issym() and not _link_inside(name, m.linkname):
                    outside.append("symlink escapes the archive: %s -> %s" % (name, m.linkname))
                elif not (m.isdir() or m.isreg() or m.issym()):
                    outside.append("special member not allowed: " + name)
                # End-if
                if name == TOP:
                    continue
                # End-if
                if not name.startswith(TOP + "/"):
                    outside.append("member outside %s/: %s" % (TOP, name))
                    continue
                # End-if
                members[name[len(TOP) + 1:]] = (m.isdir(), m.mode)
            # End-for
        # End-with
    elif path.endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad:
                outside.append("corrupt zip member: " + bad)
            # End-if
            for info in zf.infolist():
                name = info.filename.rstrip("/")
                if name == TOP:
                    continue
                # End-if
                if not name.startswith(TOP + "/"):
                    outside.append("member outside %s/: %s" % (TOP, name))
                    continue
                # End-if
                mode = (info.external_attr >> 16) or None
                members[name[len(TOP) + 1:]] = (info.is_dir(), mode)
            # End-for
        # End-with
    else:
        raise ContractError("unknown archive type: " + path)
    # End-if
    return members, outside
# End-def


##
 # @brief Pack src into the canonical archive; verify the result.
 #
 # @return archive_path
 #
def pack(src, out_dir, target_id, version, platform):
    if not os.path.isdir(src):
        raise ContractError("source dir not found: " + src)
    # End-if
    entries = list(_walk(src))
    src_members = {}
    for rel, path, is_dir in entries:
        if os.path.islink(path):
            target = os.readlink(path)
            if platform != "linux" or not _link_inside(rel, target):
                raise ContractError("non-portable symlink in artifact tree: %s -> %s" % (rel, target))
            # End-if
        # End-if
        src_members[rel] = (is_dir, os.lstat(path).st_mode if platform == "linux" else None)
    # End-for
    problems = check_members(src_members, platform)
    if problems:
        raise ContractError("artifact tree violates the layout contract:\n  - " + "\n  - ".join(problems))
    # End-if

    os.makedirs(out_dir, exist_ok=True)
    name = archive_name(target_id, version, platform)
    out = os.path.join(out_dir, name)
    if os.path.exists(out):
        os.remove(out)
    # End-if

    if platform == "linux":
        def norm(ti):
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            return ti
        # End-def
        with tarfile.open(out, "w:gz", compresslevel=9) as tf:
            tf.add(src, arcname=TOP, recursive=False, filter=norm)
            for rel, path, _is_dir in entries:
                tf.add(path, arcname=TOP + "/" + rel, recursive=False, filter=norm)
            # End-for
        # End-with
    else:
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            zf.write(src, TOP + "/")
            for rel, path, is_dir in entries:
                zf.write(path, TOP + "/" + rel + ("/" if is_dir else ""))
            # End-for
        # End-with
    # End-if

    verify(out, platform)
    return out
# End-def


##
 # @brief Verify one archive (layout and modes).
 #
def verify(path, platform):
    problems = []
    members, outside = read_members(path)
    problems += outside
    problems += check_members(members, platform)
    if problems:
        raise ContractError("%s:\n  - %s" % (os.path.basename(path), "\n  - ".join(problems)))
    # End-if
    return len(members)
# End-def


##
 # @brief Verify the complete release set: exactly one archive per target.
 #
 # @return list of archive names in target order.
 #
def verify_set(directory, version, targets):
    expected = {}
    for t in targets:
        plat = "windows" if t.startswith("win-") else "linux"
        expected[archive_name(t, version, plat)] = plat
    # End-for
    if len(expected) != len(targets):
        raise ContractError("duplicate target ids in the expected list")
    # End-if
    present = sorted(f for f in os.listdir(directory)
                     if f.endswith(".tar.gz") or f.endswith(".zip"))
    missing = sorted(set(expected) - set(present))
    extra = sorted(set(present) - set(expected))
    problems = ["missing archive: " + m for m in missing] + ["unexpected archive: " + e for e in extra]
    rows = []
    for name in sorted(expected):
        if name in missing:
            continue
        # End-if
        path = os.path.join(directory, name)
        try:
            verify(path, expected[name])
            rows.append(name)
        except ContractError as e:
            problems.append(str(e))
        # End-try
    # End-for
    if problems:
        raise ContractError("release set check failed:\n  - " + "\n  - ".join(problems))
    # End-if
    return rows
# End-def


def main(argv=None):
    ap = argparse.ArgumentParser(description="Dandelion release archive contract")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pack")
    p.add_argument("--src", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--target-id", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--platform", choices=sorted(EXT), required=True)
    p.add_argument("--github-output", default=None,
                   help="append archive=/size= lines to this file (GITHUB_OUTPUT)")
    v = sub.add_parser("verify")
    v.add_argument("--archive", required=True)
    v.add_argument("--platform", choices=sorted(EXT), required=True)
    s = sub.add_parser("verify-set")
    s.add_argument("--dir", required=True)
    s.add_argument("--version", required=True)
    s.add_argument("--targets", required=True, help="comma/space separated target ids")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "pack":
            out = pack(args.src, args.out_dir, args.target_id, args.version, args.platform)
            size = os.path.getsize(out)
            print("[+] Packed %s (%d bytes = %.1f MiB)" % (out, size, size / 1048576.0))
            if args.github_output:
                with open(args.github_output, "a", encoding="utf-8") as f:
                    f.write("archive=%s\nsize=%d\n" % (os.path.basename(out), size))
                # End-with
            # End-if
        elif args.cmd == "verify":
            count = verify(args.archive, args.platform)
            print("[+] %s OK (%d members)" % (os.path.basename(args.archive), count))
        else:
            targets = [t for t in re.split(r"[\s,]+", args.targets) if t]
            rows = verify_set(args.dir, args.version, targets)
            for name in rows:
                print("[+] %s" % name)
            # End-for
            print("[+] Release set OK: %d archives for version %s" % (len(rows), args.version))
        # End-if
    except ContractError as e:
        print("[-] " + str(e))
        return 1
    # End-try
    return 0
# End-def


if __name__ == "__main__":
    sys.exit(main())
# End-if
