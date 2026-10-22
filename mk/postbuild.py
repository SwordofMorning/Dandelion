##
 # @file mk/postbuild.py
 # @date 2026/10/22
 #
 # @brief Build-time post-processing helpers for mk/make.py (never shipped).
 #
 # - ELF inspection (pure Python, no dependency on `file`/`readelf`).
 # - Safe strip with a per-object ledger and dynamic-section verification.
 # - MANIFEST.txt generation (path, size, sha256, mode, link, owner package).
 # - Forbidden-package audit (file tree + Nuitka report).
 #
 # @note All functions are deterministic and side-effect free except where the
 #   name says otherwise (strip_tree, write_manifest), so they can be unit
 #   tested offline on synthetic trees.
 #

import hashlib
import os
import re
import shutil
import stat
import struct
import subprocess

ELF_MAGIC = b"\x7fELF"
ET_EXEC = 2
ET_DYN = 3
SHT_DYNAMIC = 6
DT_NULL, DT_NEEDED, DT_SONAME, DT_RPATH, DT_RUNPATH = 0, 1, 14, 15, 29

##
 # @brief Read a NUL-terminated string at offset from a string table.
 #
def _cstr(table, off):
    if off >= len(table):
        return b""
    # End-if
    end = table.find(b"\0", off)
    return table[off:] if end < 0 else table[off:end]
# End-def

##
 # @brief Parse the ELF header and section table of a file.
 #
 # @param path File path.
 #
 # @return dict(type=int, sections=set[str], dynamic=dict) or None when the
 #   file is not an ELF object. Raises ValueError on a truncated/corrupt ELF.
 #
def elf_info(path):
    with open(path, "rb") as f:
        ident = f.read(16)
        if len(ident) < 16 or ident[:4] != ELF_MAGIC:
            return None
        # End-if
        ei_class, ei_data = ident[4], ident[5]
        if ei_class not in (1, 2) or ei_data not in (1, 2):
            raise ValueError("unsupported ELF class/data")
        # End-if
        end = "<" if ei_data == 1 else ">"
        is64 = ei_class == 2
        if is64:
            hdr_fmt = end + "HHIQQQIHHHHHH"
        else:
            hdr_fmt = end + "HHIIIIIHHHHHH"
        # End-if
        hdr = f.read(struct.calcsize(hdr_fmt))
        if len(hdr) != struct.calcsize(hdr_fmt):
            raise ValueError("truncated ELF header")
        # End-if
        (e_type, _machine, _version, _entry, _phoff, e_shoff, _flags, _ehsize,
         _phentsize, _phnum, e_shentsize, e_shnum, e_shstrndx) = struct.unpack(hdr_fmt, hdr)

        sh_fmt = end + ("IIQQQQIIQQ" if is64 else "IIIIIIIIII")
        sh_size = struct.calcsize(sh_fmt)
        sections = []
        if e_shoff and e_shnum:
            if e_shentsize < sh_size:
                raise ValueError("bad section header size")
            # End-if
            for i in range(e_shnum):
                f.seek(e_shoff + i * e_shentsize)
                raw = f.read(sh_size)
                if len(raw) != sh_size:
                    raise ValueError("truncated section table")
                # End-if
                (name, typ, _fl, _addr, off, size, link, _info, _align, entsize) = struct.unpack(sh_fmt, raw)
                sections.append({"name_off": name, "type": typ, "offset": off,
                                 "size": size, "link": link, "entsize": entsize})
            # End-for
        # End-if

        def read_at(off, size):
            f.seek(off)
            data = f.read(size)
            if len(data) != size:
                raise ValueError("truncated section data")
            # End-if
            return data
        # End-def

        names = set()
        if sections and e_shstrndx < len(sections):
            strtab = read_at(sections[e_shstrndx]["offset"], sections[e_shstrndx]["size"])
            for s in sections:
                names.add(_cstr(strtab, s["name_off"]).decode("ascii", "replace"))
            # End-for
        # End-if

        dynamic = {"NEEDED": [], "SONAME": None, "RPATH": None, "RUNPATH": None}
        dyn = [s for s in sections if s["type"] == SHT_DYNAMIC]
        if dyn and dyn[0]["link"] < len(sections):
            d = dyn[0]
            dynstr_sec = sections[d["link"]]
            dynstr = read_at(dynstr_sec["offset"], dynstr_sec["size"])
            ent_fmt = end + ("qQ" if is64 else "iI")
            ent_size = struct.calcsize(ent_fmt)
            data = read_at(d["offset"], d["size"])
            for pos in range(0, len(data) - ent_size + 1, ent_size):
                tag, val = struct.unpack_from(ent_fmt, data, pos)
                if tag == DT_NULL:
                    break
                # End-if
                if tag in (DT_NEEDED, DT_SONAME, DT_RPATH, DT_RUNPATH):
                    text = _cstr(dynstr, val).decode("utf-8", "replace")
                    if tag == DT_NEEDED:
                        dynamic["NEEDED"].append(text)
                    else:
                        key = {DT_SONAME: "SONAME", DT_RPATH: "RPATH", DT_RUNPATH: "RUNPATH"}[tag]
                        dynamic[key] = text
                    # End-if
                # End-if
            # End-for
        # End-if
        return {"type": e_type, "sections": names, "dynamic": dynamic}
    # End-with
# End-def

##
 # @brief True if the ELF still carries a symbol table or debug sections.
 #
def has_strippable_sections(info):
    return any(n == ".symtab" or n.startswith(".debug") or n.startswith(".zdebug")
               for n in info["sections"])
# End-def

##
 # @brief Enumerate regular files under root (no symlinks), sorted, NUL-safe.
 #
def iter_files(root):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for fn in sorted(filenames):
            path = os.path.join(dirpath, fn)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            # End-if
            yield path
        # End-for
    # End-for
# End-def

##
 # @brief Strip ELF executables / shared objects under bin_dir, safely.
 #
 # For every regular file: non-ELF -> ignored; ELF other than EXEC/DYN ->
 # SKIP-TYPE; no .symtab/.debug -> SKIP-STRIPPED; otherwise the object is
 # copied to a temp file, stripped with `strip --strip-unneeded`, re-parsed,
 # and its dynamic section (NEEDED/SONAME/RPATH/RUNPATH) compared with the
 # original. Only a verified result replaces the file (STRIPPED). Any error
 # leaves the original in place (FAILED-KEPT) unless on_error == "fail".
 #
 # @param bin_dir    Directory to process.
 # @param backup_dir Originals of every replaced object are copied here.
 # @param ledger     Path of the TSV ledger to write.
 # @param strip_bin  strip executable (default: found on PATH).
 # @param on_error   "keep" or "fail".
 #
 # @return dict summary with counters and byte totals.
 #
def strip_tree(bin_dir, backup_dir, ledger, strip_bin=None, on_error="keep"):
    strip_bin = strip_bin or shutil.which("strip")
    if not strip_bin:
        raise RuntimeError("strip not found on PATH (install binutils) - set [strip] enabled=false to skip")
    # End-if
    version = subprocess.run([strip_bin, "--version"], capture_output=True, text=True).stdout.splitlines()
    rows = []
    summary = {"tool": version[0] if version else strip_bin, "elf": 0, "stripped": 0,
               "skipped": 0, "failed": 0, "bytes_before": 0, "bytes_after": 0}
    failures = []

    for path in iter_files(bin_dir):
        rel = os.path.relpath(path, bin_dir)
        try:
            info = elf_info(path)
        except (OSError, ValueError) as e:
            rows.append((rel, "?", 0, 0, "FAILED-KEPT", "parse: %s" % e))
            summary["failed"] += 1
            failures.append(rel)
            continue
        # End-try
        if info is None:
            continue
        # End-if
        summary["elf"] += 1
        size = os.path.getsize(path)
        summary["bytes_before"] += size
        kind = {ET_EXEC: "EXEC", ET_DYN: "DYN"}.get(info["type"], "TYPE%d" % info["type"])
        if info["type"] not in (ET_EXEC, ET_DYN):
            rows.append((rel, kind, size, size, "SKIP-TYPE", ""))
            summary["skipped"] += 1
            summary["bytes_after"] += size
            continue
        # End-if
        if not has_strippable_sections(info):
            rows.append((rel, kind, size, size, "SKIP-STRIPPED", "no .symtab/.debug"))
            summary["skipped"] += 1
            summary["bytes_after"] += size
            continue
        # End-if

        tmp = path + ".strip-tmp"
        status, note, new_size = "FAILED-KEPT", "", size
        try:
            shutil.copy2(path, tmp)
            res = subprocess.run([strip_bin, "--strip-unneeded", tmp], capture_output=True, text=True,
                                 env=dict(os.environ, LC_ALL="C"))
            if res.returncode != 0:
                note = "strip rc=%d: %s" % (res.returncode, (res.stderr or res.stdout).strip()[:200])
            else:
                after = elf_info(tmp)
                if after is None or after["type"] != info["type"]:
                    note = "stripped output is not the same ELF type"
                elif after["dynamic"] != info["dynamic"]:
                    note = "dynamic section changed: %r -> %r" % (info["dynamic"], after["dynamic"])
                else:
                    backup = os.path.join(backup_dir, rel)
                    os.makedirs(os.path.dirname(backup), exist_ok=True)
                    shutil.copy2(path, backup)
                    mode = stat.S_IMODE(os.stat(path).st_mode)
                    os.replace(tmp, path)
                    os.chmod(path, mode)
                    new_size = os.path.getsize(path)
                    status = "STRIPPED"
                # End-if
            # End-if
        except (OSError, ValueError) as e:
            note = "%s: %s" % (type(e).__name__, e)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
            # End-if
        # End-try
        rows.append((rel, kind, size, new_size, status, note))
        summary["bytes_after"] += new_size
        if status == "STRIPPED":
            summary["stripped"] += 1
        else:
            summary["failed"] += 1
            failures.append(rel)
        # End-if
    # End-for

    os.makedirs(os.path.dirname(os.path.abspath(ledger)), exist_ok=True)
    with open(ledger, "w", encoding="utf-8", newline="\n") as f:
        f.write("# tool: %s\n" % summary["tool"])
        f.write("path\tkind\tbytes_before\tbytes_after\tstatus\tnote\n")
        for r in rows:
            f.write("\t".join(str(x).replace("\t", " ").replace("\n", " ") for x in r) + "\n")
        # End-for
    # End-with
    summary["failures"] = failures
    if failures and on_error == "fail":
        raise RuntimeError("strip failed for %d object(s): %s" % (len(failures), ", ".join(failures[:10])))
    # End-if
    return summary
# End-def

##
 # @brief Top-level owner of a path inside bin/ (package / dist-info / file).
 #
def _owner(rel):
    parts = rel.replace(os.sep, "/").split("/")
    if parts[0] == "_stdlib_fallback":
        return "stdlib-fallback:" + (parts[1].split(".")[0] if len(parts) > 1 else "")
    # End-if
    if len(parts) == 1:
        return "nuitka:" + parts[0]
    # End-if
    head = parts[0]
    if head.endswith(".dist-info"):
        return "dist-info:" + re.split(r"-\d", head[:-len(".dist-info")], 1)[0]
    # End-if
    return "package:" + head
# End-def

##
 # @brief Write MANIFEST.txt describing every entry of the artifact tree.
 #
 # Columns (TSV): path (relative to target_dir, '/' separated), type (f/l),
 # bytes, sha256, mode (octal), link target, owner. MANIFEST.txt itself is
 # excluded (no self-reference); its own digest is returned to the caller.
 #
 # @return (manifest_path, sha256_of_manifest, entries_count)
 #
def write_manifest(target_dir, manifest_rel="bin/MANIFEST.txt", header=()):
    manifest = os.path.join(target_dir, manifest_rel.replace("/", os.sep))
    entries = []
    for dirpath, dirnames, filenames in os.walk(target_dir, followlinks=False):
        dirnames.sort()
        for fn in sorted(filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]):
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, target_dir).replace(os.sep, "/")
            if rel == manifest_rel:
                continue
            # End-if
            st = os.lstat(path)
            mode = "%04o" % stat.S_IMODE(st.st_mode)
            bin_rel = rel[4:] if rel.startswith("bin/") else None
            owner = _owner(bin_rel) if bin_rel else "workspace"
            if os.path.islink(path):
                entries.append((rel, "l", 0, "-", mode, os.readlink(path), owner))
                continue
            # End-if
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
                # End-for
            # End-with
            entries.append((rel, "f", st.st_size, h.hexdigest(), mode, "-", owner))
        # End-for
    # End-for
    entries.sort()
    total = sum(e[2] for e in entries)
    with open(manifest, "w", encoding="utf-8", newline="\n") as f:
        for line in header:
            f.write("# " + line + "\n")
        # End-for
        f.write("# entries: %d, total bytes: %d\n" % (len(entries), total))
        f.write("path\ttype\tbytes\tsha256\tmode\tlink\towner\n")
        for e in entries:
            f.write("\t".join(str(x) for x in e) + "\n")
        # End-for
    # End-with
    with open(manifest, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    # End-with
    return manifest, digest, len(entries)
# End-def

##
 # @brief Summarize bytes per owner (top-level package) for the build log.
 #
def owner_sizes(manifest_path):
    sizes = {}
    with open(manifest_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("#") or line.startswith("path\t"):
                continue
            # End-if
            cols = line.rstrip("\n").split("\t")
            if len(cols) == 7 and cols[1] == "f":
                sizes[cols[6]] = sizes.get(cols[6], 0) + int(cols[2])
            # End-if
        # End-for
    # End-with
    return sorted(sizes.items(), key=lambda kv: -kv[1])
# End-def

##
 # @brief Find forbidden top-level packages in the artifact tree and report.
 #
 # Matches (case-insensitive) a top-level entry of bin/ (or of
 # bin/_stdlib_fallback) named <pkg>, <pkg>.py, <pkg>-*.dist-info, <pkg>.libs,
 # <pkg>-*.data, and compiled extension modules <pkg>.*.so/.pyd; plus module
 # names <pkg> / <pkg>.* in the Nuitka XML report when given.
 #
 # @return List of human-readable findings (empty = clean).
 #
def audit_forbidden(bin_dir, forbidden, report_path=None):
    findings = []
    names = [p.strip().lower() for p in forbidden if p.strip()]
    if not names:
        return findings
    # End-if
    roots = [bin_dir, os.path.join(bin_dir, "_stdlib_fallback")]
    for root in roots:
        if not os.path.isdir(root):
            continue
        # End-if
        for entry in sorted(os.listdir(root)):
            low = entry.lower()
            for pkg in names:
                if (low == pkg or low == pkg + ".py" or low == pkg + ".libs"
                        or (low.startswith(pkg + "-") and (low.endswith(".dist-info") or low.endswith(".data")))
                        or (low.startswith(pkg + ".") and (low.endswith(".so") or low.endswith(".pyd")))):
                    findings.append("tree: %s" % os.path.relpath(os.path.join(root, entry), bin_dir))
                # End-if
            # End-for
        # End-for
    # End-for
    if report_path and os.path.isfile(report_path):
        with open(report_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        # End-with
        for pkg in names:
            pat = re.compile(r'<module\b[^>]*\bname="(%s(?:\.[^"]*)?)"' % re.escape(pkg), re.IGNORECASE)
            mods = sorted(set(pat.findall(text)))
            if mods:
                findings.append("nuitka-report: %s (%d modules, e.g. %s)" % (pkg, len(mods), mods[0]))
            # End-if
        # End-for
    # End-if
    return findings
# End-def
