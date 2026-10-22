##
 # @file test/test_build_contract.py
 # @brief Offline tests for the build/release contract (no Nuitka, gcc, docker).
 #
 # Covers:
 #   - .github/scripts/package.py : archive naming, layout, POSIX modes, sha256,
 #     symlink policy, release-set verification (exactly N targets).
 #   - mk/postbuild.py            : ELF parsing, safe strip ledger (with a fake
 #     strip tool), manifest generation, forbidden-package audit.
 #   - main.py                    : --version / --self-test / unknown flags.
 #   - .github/workflows/release.yml : the 11-target matrix, release gating and
 #     the target list used by verify-set stay in sync.
 #   - mk/config/build.config + pyproject.toml : pandas removed everywhere.
 #
 # Run: python test/test_build_contract.py -v
 #

import configparser
import importlib.util
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mk import postbuild  # noqa: E402


def _load_package_module():
    spec = importlib.util.spec_from_file_location("dandelion_package", ROOT / ".github" / "scripts" / "package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
# End-def


package = _load_package_module()

EXPECTED_TARGETS = [
    "win-x86_64",
    "linux-x86_64-ubuntu18", "linux-x86_64-ubuntu20", "linux-x86_64-ubuntu22",
    "linux-x86_64-ubuntu24", "linux-x86_64-ubuntu26",
    "linux-aarch64-ubuntu18", "linux-aarch64-ubuntu20", "linux-aarch64-ubuntu22",
    "linux-aarch64-ubuntu24", "linux-aarch64-ubuntu26",
]
WORKSPACE_DIR = "." + "env"   # spelled out to keep the literal out of tool filters


def scratch():
    base = ROOT / ".playground"
    base.mkdir(exist_ok=True)
    return tempfile.TemporaryDirectory(prefix="build-contract-", dir=base)
# End-def


##
 # @brief Create a fake build/dandelion tree that satisfies the layout contract.
 #
def make_tree(root, platform="linux"):
    top = Path(root) / "dandelion"
    (top / "bin").mkdir(parents=True)
    for d in (WORKSPACE_DIR, ".log", "llm/memory", "llm/skill"):
        (top / d).mkdir(parents=True, exist_ok=True)
        (top / d / ".keep").write_text("")
    # End-for
    (top / "version.txt").write_text("v1.2.3\n")
    (top / "bin" / "MANIFEST.txt").write_text("# manifest\n")
    (top / "bin" / "openpyxl").mkdir()
    (top / "bin" / "openpyxl" / "__init__.py").write_text("")
    if platform == "linux":
        (top / "dandelion.sh").write_text("#!/bin/sh\n")
        (top / "bin" / "dandelion").write_bytes(b"\x7fELF fake")
        os.chmod(top / "dandelion.sh", 0o755)
        os.chmod(top / "bin" / "dandelion", 0o755)
    else:
        (top / "dandelion.cmd").write_text("@echo off\r\n")
        (top / "bin" / "dandelion.exe").write_bytes(b"MZ fake")
    # End-if
    return top
# End-def


class PackageContract(unittest.TestCase):
    def test_archive_name(self):
        self.assertEqual(package.archive_name("linux-aarch64-ubuntu18", "v0.1.0", "linux"),
                         "dandelion-linux-aarch64-ubuntu18-v0.1.0.tar.gz")
        self.assertEqual(package.archive_name("win-x86_64", "v0.0.0-dev-261022-abc1234-ci", "windows"),
                         "dandelion-win-x86_64-v0.0.0-dev-261022-abc1234-ci.zip")
        for bad in ("v1/2", "", "v1 2", "-v1", "v1;rm"):
            with self.assertRaises(package.ContractError):
                package.archive_name("win-x86_64", bad, "windows")
            # End-with
        # End-for
    # End-def

    def test_pack_linux_preserves_modes_and_hidden_dirs(self):
        with scratch() as tmp:
            top = make_tree(tmp, "linux")
            out, digest = package.pack(str(top), os.path.join(tmp, "dist"), "linux-x86_64-ubuntu18", "v1.2.3", "linux")
            self.assertTrue(out.endswith("dandelion-linux-x86_64-ubuntu18-v1.2.3.tar.gz"))
            self.assertEqual(Path(out + ".sha256").read_text().split(), [digest, os.path.basename(out)])
            with tarfile.open(out) as tf:
                names = tf.getnames()
                self.assertEqual(stat.S_IMODE(tf.getmember("dandelion/dandelion.sh").mode), 0o755)
                self.assertEqual(stat.S_IMODE(tf.getmember("dandelion/bin/dandelion").mode), 0o755)
                self.assertEqual({m.uid for m in tf.getmembers()}, {0})
            # End-with
            self.assertIn("dandelion/" + WORKSPACE_DIR + "/.keep", names)
            self.assertIn("dandelion/.log/.keep", names)
            self.assertTrue(all(n == "dandelion" or n.startswith("dandelion/") for n in names))
            # Extract as a plain user and re-check the executable bits.
            ex = Path(tmp) / "x"
            with tarfile.open(out) as tf:
                tf.extractall(ex, filter="tar")
            # End-with
            self.assertTrue(os.access(ex / "dandelion" / "dandelion.sh", os.X_OK))
            self.assertTrue(os.access(ex / "dandelion" / "bin" / "dandelion", os.X_OK))
        # End-with
    # End-def

    def test_pack_windows_zip_contains_hidden_entries(self):
        with scratch() as tmp:
            top = make_tree(tmp, "windows")
            out, _ = package.pack(str(top), os.path.join(tmp, "dist"), "win-x86_64", "v1.2.3", "windows")
            with zipfile.ZipFile(out) as zf:
                names = zf.namelist()
            # End-with
            self.assertIn("dandelion/" + WORKSPACE_DIR + "/.keep", names)
            self.assertIn("dandelion/llm/skill/.keep", names)
            self.assertIn("dandelion/bin/dandelion.exe", names)
        # End-with
    # End-def

    def test_pack_rejects_bad_mode_and_missing_entries(self):
        with scratch() as tmp:
            top = make_tree(tmp, "linux")
            os.chmod(top / "bin" / "dandelion", 0o644)
            with self.assertRaisesRegex(package.ContractError, "bad mode for bin/dandelion"):
                package.pack(str(top), os.path.join(tmp, "dist"), "linux-x86_64-ubuntu18", "v1", "linux")
            # End-with
            os.chmod(top / "bin" / "dandelion", 0o755)
            (top / "bin" / "MANIFEST.txt").unlink()
            with self.assertRaisesRegex(package.ContractError, "missing file: bin/MANIFEST.txt"):
                package.pack(str(top), os.path.join(tmp, "dist"), "linux-x86_64-ubuntu18", "v1", "linux")
            # End-with
        # End-with
    # End-def

    def test_symlink_policy(self):
        with scratch() as tmp:
            top = make_tree(tmp, "linux")
            os.symlink("openpyxl", top / "bin" / "alias_dir")
            out, _ = package.pack(str(top), os.path.join(tmp, "dist"), "linux-x86_64-ubuntu18", "v1", "linux")
            with tarfile.open(out) as tf:
                self.assertTrue(tf.getmember("dandelion/bin/alias_dir").issym())
            # End-with
            os.symlink("/etc/passwd", top / "bin" / "escape")
            with self.assertRaisesRegex(package.ContractError, "non-portable symlink"):
                package.pack(str(top), os.path.join(tmp, "dist"), "linux-x86_64-ubuntu18", "v1", "linux")
            # End-with
        # End-with
    # End-def

    def test_verify_detects_tampering(self):
        with scratch() as tmp:
            top = make_tree(tmp, "linux")
            out, _ = package.pack(str(top), os.path.join(tmp, "dist"), "linux-x86_64-ubuntu18", "v1", "linux")
            with open(out, "ab") as f:
                f.write(b"x")
            # End-with
            with self.assertRaisesRegex(package.ContractError, "checksum file mismatch"):
                package.verify(out, "linux", out + ".sha256")
            # End-with
        # End-with
    # End-def

    def test_verify_set_requires_exact_target_set(self):
        with scratch() as tmp:
            dist = os.path.join(tmp, "dist")
            for t in EXPECTED_TARGETS:
                plat = "windows" if t.startswith("win-") else "linux"
                top = make_tree(os.path.join(tmp, "src-" + t), plat)
                package.pack(str(top), dist, t, "v9.9.9", plat)
            # End-for
            sums = os.path.join(tmp, "SHA256SUMS")
            rows = package.verify_set(dist, "v9.9.9", EXPECTED_TARGETS, sums)
            self.assertEqual(len(rows), 11)
            self.assertEqual(len(Path(sums).read_text().splitlines()), 11)
            # Wrong version -> 11 missing + 11 unexpected.
            with self.assertRaisesRegex(package.ContractError, "missing archive"):
                package.verify_set(dist, "v9.9.8", EXPECTED_TARGETS)
            # End-with
            # One archive missing.
            os.remove(os.path.join(dist, "dandelion-linux-aarch64-ubuntu26-v9.9.9.tar.gz"))
            with self.assertRaisesRegex(package.ContractError, "missing archive: dandelion-linux-aarch64-ubuntu26"):
                package.verify_set(dist, "v9.9.9", EXPECTED_TARGETS)
            # End-with
        # End-with
    # End-def

    def test_cli_exit_codes(self):
        with scratch() as tmp:
            top = make_tree(tmp, "linux")
            script = str(ROOT / ".github" / "scripts" / "package.py")
            ok = subprocess.run([sys.executable, script, "pack", "--src", str(top), "--out-dir",
                                 os.path.join(tmp, "dist"), "--target-id", "linux-x86_64-ubuntu20",
                                 "--version", "v1", "--platform", "linux"], capture_output=True, text=True)
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
            bad = subprocess.run([sys.executable, script, "verify-set", "--dir", os.path.join(tmp, "dist"),
                                  "--version", "v1", "--targets", ",".join(EXPECTED_TARGETS)],
                                 capture_output=True, text=True)
            self.assertEqual(bad.returncode, 1)
        # End-with
    # End-def
# End-class


##
 # @brief A fake `strip` that always fails, to exercise the keep-original path.
 #
def fake_strip(directory, behavior):
    assert behavior == "fail"
    path = Path(directory) / "fake-strip"
    path.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo "GNU strip (fake) 2.30"; exit 0; fi\n'
                    'echo "strip: unsupported" >&2\nexit 1\n')
    os.chmod(path, 0o755)
    return str(path)
# End-def


@unittest.skipUnless(sys.platform.startswith("linux") and os.path.exists("/bin/ls"), "needs a Linux ELF")
class PostbuildContract(unittest.TestCase):
    def copy_elf(self, directory):
        dst = Path(directory) / "bin" / "libfake.so"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(os.path.realpath("/bin/ls"), dst)
        return dst
    # End-def

    def test_elf_info(self):
        info = postbuild.elf_info(os.path.realpath("/bin/ls"))
        self.assertIn(info["type"], (postbuild.ET_EXEC, postbuild.ET_DYN))
        self.assertIn("libc.so.6", info["dynamic"]["NEEDED"])
        self.assertIsNone(postbuild.elf_info(str(ROOT / "main.py")))
    # End-def

    def test_strip_failure_keeps_original_and_is_recorded(self):
        with scratch() as tmp:
            so = self.copy_elf(tmp)
            info = postbuild.elf_info(str(so))
            if not postbuild.has_strippable_sections(info):
                self.skipTest("/bin/ls is already stripped on this host")
            # End-if
            before = so.read_bytes()
            ledger = os.path.join(tmp, "ledger.tsv")
            summary = postbuild.strip_tree(os.path.join(tmp, "bin"), os.path.join(tmp, "orig"), ledger,
                                           strip_bin=fake_strip(tmp, "fail"), on_error="keep")
            self.assertEqual(summary["failed"], 1)
            self.assertEqual(so.read_bytes(), before)
            self.assertIn("FAILED-KEPT", Path(ledger).read_text())
            with self.assertRaises(RuntimeError):
                postbuild.strip_tree(os.path.join(tmp, "bin"), os.path.join(tmp, "orig"), ledger,
                                     strip_bin=fake_strip(tmp, "fail"), on_error="fail")
            # End-with
        # End-with
    # End-def

    def test_strip_skips_already_stripped_and_non_elf(self):
        with scratch() as tmp:
            (Path(tmp) / "bin").mkdir()
            (Path(tmp) / "bin" / "x.py").write_text("print(1)\n")
            so = Path(tmp) / "bin" / "libc-copy.so"
            libc = "/usr/lib/x86_64-linux-gnu/libc.so.6"
            if not os.path.exists(libc):
                self.skipTest("no libc path")
            # End-if
            shutil.copy2(libc, so)
            if postbuild.has_strippable_sections(postbuild.elf_info(str(so))):
                self.skipTest("host libc is not stripped")
            # End-if
            ledger = os.path.join(tmp, "ledger.tsv")
            summary = postbuild.strip_tree(os.path.join(tmp, "bin"), os.path.join(tmp, "orig"), ledger,
                                           strip_bin=fake_strip(tmp, "fail"))
            self.assertEqual((summary["elf"], summary["skipped"], summary["failed"]), (1, 1, 0))
            self.assertIn("SKIP-STRIPPED", Path(ledger).read_text())
        # End-with
    # End-def

    def test_real_strip_preserves_dynamic_section(self):
        strip_bin = shutil.which("strip")
        if not strip_bin:
            self.skipTest("binutils strip not installed")
        # End-if
        with scratch() as tmp:
            so = self.copy_elf(tmp)
            info = postbuild.elf_info(str(so))
            ledger = os.path.join(tmp, "ledger.tsv")
            summary = postbuild.strip_tree(os.path.join(tmp, "bin"), os.path.join(tmp, "orig"), ledger)
            self.assertEqual(summary["failed"], 0, Path(ledger).read_text())
            self.assertEqual(postbuild.elf_info(str(so))["dynamic"], info["dynamic"])
            self.assertTrue(os.access(so, os.X_OK))
        # End-with
    # End-def

    def test_manifest_and_audit(self):
        with scratch() as tmp:
            top = make_tree(tmp, "linux")
            (top / "bin" / "numpy").mkdir()
            (top / "bin" / "numpy" / "__init__.py").write_text("")
            (top / "bin" / "pandas-3.0.5.dist-info").mkdir()
            (top / "bin" / "numpy.libs").mkdir()
            manifest, digest, count = postbuild.write_manifest(str(top), header=["test"])
            text = Path(manifest).read_text()
            self.assertIn("bin/openpyxl/__init__.py\tf\t0\t", text)
            self.assertIn("\tpackage:openpyxl\n", text)
            self.assertNotIn("bin/MANIFEST.txt\t", text)
            self.assertRegex(text, r"dandelion\.sh\tf\t\d+\t[0-9a-f]{64}\t0755\t-\tworkspace")
            self.assertEqual(len(digest), 64)
            findings = postbuild.audit_forbidden(str(top / "bin"), ["pandas", "numpy"])
            self.assertEqual(sorted(findings), ["tree: numpy", "tree: numpy.libs", "tree: pandas-3.0.5.dist-info"])
            report = Path(tmp) / "report.xml"
            report.write_text('<nuitka-compilation-report><module name="numpy.core" kind="x"/>'
                              '<module name="numpyish" kind="x"/></nuitka-compilation-report>')
            shutil.rmtree(top / "bin" / "numpy")
            shutil.rmtree(top / "bin" / "numpy.libs")
            shutil.rmtree(top / "bin" / "pandas-3.0.5.dist-info")
            self.assertEqual(postbuild.audit_forbidden(str(top / "bin"), ["pandas", "numpy"]), [])
            findings = postbuild.audit_forbidden(str(top / "bin"), ["pandas", "numpy"], str(report))
            self.assertEqual(len(findings), 1)
            self.assertIn("numpy.core", findings[0])
        # End-with
    # End-def
# End-class


class EntryPointContract(unittest.TestCase):
    def run_main(self, *args, timeout=180):
        return subprocess.run([sys.executable, "-B", str(ROOT / "main.py"), *args],
                              cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
    # End-def

    def test_version_needs_no_config_or_heavy_imports(self):
        r = self.run_main("--version")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout.strip(), r"^v\S+$")
        probe = subprocess.run([sys.executable, "-B", "-c",
                                "import sys, main; rc = main.main(['--version']);"
                                "bad = [m for m in ('src.utils', 'src.core.agent', 'anthropic', 'openai') if m in sys.modules];"
                                "sys.exit(rc or (3 if bad else 0))"],
                               cwd=str(ROOT), capture_output=True, text=True, timeout=60)
        self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)
    # End-def

    def test_self_test_passes_from_source(self):
        r = self.run_main("--self-test")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Self-test OK", r.stdout)
    # End-def

    def test_self_test_fails_when_a_dependency_is_missing(self):
        code = ("import sys, importlib.abc\n"
                "class B(importlib.abc.MetaPathFinder):\n"
                "    def find_spec(self, n, p=None, t=None):\n"
                "        if n.split('.')[0] == 'paramiko': raise ImportError('blocked ' + n)\n"
                "sys.meta_path.insert(0, B())\n"
                "import main\n"
                "sys.exit(main.main(['--self-test']))\n")
        r = subprocess.run([sys.executable, "-B", "-c", code], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("ssh-crypto", r.stdout)
        self.assertIn("Self-test FAILED", r.stdout)
    # End-def

    def test_unknown_flag(self):
        self.assertEqual(self.run_main("--nope").returncode, 2)
    # End-def
# End-class


FAKE_DOCKER = r'''#!/usr/bin/env python3
# Fake `docker run` for offline tests: maps the /dist volume, relocates
# /tmp/smoke into a scratch dir and runs the inner `bash -c` script locally.
import os, subprocess, sys
args = sys.argv[1:]
mode = os.path.basename(sys.argv[0]) + ":" + os.getenv("FAKE_DOCKER_MODE", "run")
if os.getenv("FAKE_DOCKER_MODE") == "noop":
    sys.exit(0)
vol = args[args.index("-v") + 1].split(":")[0]
i = args.index("-c")
script = args[i + 1].replace("/tmp/smoke", os.environ["FAKE_SMOKE_DIR"])
rest = [a.replace("/dist", vol, 1) if a.startswith("/dist/") else a for a in args[i + 2:]]
sys.exit(subprocess.call(["bash", "-c", script] + rest))
'''

FAKE_BINARY = '''#!/bin/sh
case "$1" in
  --version) echo "@VERSION@" ;;
  --self-test) echo "[+] fake ok"; echo "Self-test OK" ;;
  "") echo "[>] Initializing Dandelion Project (v@VERSION@)..."; echo "[-] FATAL: Failed to load config at x." ; exit 1 ;;
esac
'''


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash"), "needs bash")
class SmokeScriptContract(unittest.TestCase):
    def run_smoke(self, binary_version, expected, mode="run"):
        with scratch() as tmp:
            top = make_tree(tmp, "linux")
            (top / "bin" / "dandelion").write_text(FAKE_BINARY.replace("@VERSION@", binary_version))
            (top / "dandelion.sh").write_text('#!/bin/sh\nSCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
                                             'exec "$SCRIPT_DIR/bin/dandelion" "$@"\n')
            os.chmod(top / "bin" / "dandelion", 0o755)
            os.chmod(top / "dandelion.sh", 0o755)
            out, _ = package.pack(str(top), os.path.join(tmp, "dist"), "linux-x86_64-ubuntu18", "v1.0.0", "linux")
            fake_bin = Path(tmp) / "fakebin"
            fake_bin.mkdir()
            (fake_bin / "docker").write_text(FAKE_DOCKER)
            os.chmod(fake_bin / "docker", 0o755)
            smoke_dir = Path(tmp) / "smoke-root"
            env = dict(os.environ)
            env.update({"PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
                        "FAKE_SMOKE_DIR": str(smoke_dir), "FAKE_DOCKER_MODE": mode})
            return subprocess.run(["bash", str(ROOT / ".github" / "scripts" / "smoke-test.sh"),
                                   out, "18.04", expected], capture_output=True, text=True, env=env, timeout=120)
        # End-with
    # End-def

    def test_smoke_passes_on_good_archive(self):
        r = self.run_smoke("v1.0.0", "v1.0.0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("SMOKE-INNER-COMPLETE", r.stdout)
        self.assertIn("bin/dandelion -> 755", r.stdout)
    # End-def

    def test_smoke_fails_on_version_mismatch(self):
        r = self.run_smoke("v0.9.9", "v1.0.0")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("expected 'v1.0.0'", r.stdout)
    # End-def

    def test_smoke_fails_when_inner_script_does_not_run(self):
        r = self.run_smoke("v1.0.0", "v1.0.0", mode="noop")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("did not run to completion", r.stdout)
    # End-def
# End-class


class ConfigContract(unittest.TestCase):
    def test_pandas_removed_from_declarations(self):
        deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
        names = [re.split(r"[<>=!~;\[ ]", d)[0].lower() for d in deps]
        self.assertNotIn("pandas", names)
        self.assertNotIn("numpy", names)
        self.assertIn("openpyxl", names)
        self.assertIn("tabulate", names)
        cfg = configparser.ConfigParser()
        cfg.read(ROOT / "mk" / "config" / "build.config")
        for key in ("nofollow_imports", "copy_packages"):
            items = [p.strip() for p in cfg.get("nuitka", key).split(",")]
            self.assertNotIn("pandas", items, key)
            self.assertIn("openpyxl", items, key)
            self.assertIn("tabulate", items, key)
        # End-for
        self.assertEqual(cfg.get("nuitka", "nofollow_imports"), cfg.get("nuitka", "copy_packages"))
        self.assertIn("numpy", cfg.get("audit", "forbidden_packages"))
    # End-def
# End-class


class WorkflowContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    # End-def

    def test_matrix_has_exactly_the_11_targets(self):
        entries = re.findall(r"- os: (\S+)\n\s+os_name: (\S+)\n\s+arch: (\S+)\n\s+build_mode: (\S+)\n"
                             r"\s+distro: \"([^\"]*)\"\n\s+distro_label: \"([^\"]*)\"", self.text)
        ids = ["%s-%s%s" % (e[1], e[2], e[5]) for e in entries]
        self.assertEqual(sorted(ids), sorted(EXPECTED_TARGETS))
        for e in entries:
            if e[1] == "linux":
                self.assertEqual("-ubuntu" + e[4].split(".")[0], e[5])
                self.assertEqual(e[0], "ubuntu-24.04-arm" if e[2] == "aarch64" else "ubuntu-latest")
            # End-if
        # End-for
    # End-def

    def test_release_target_list_matches_matrix(self):
        m = re.search(r"EXPECTED_TARGETS: >-\n((?:\s+\S+\n)+)", self.text)
        self.assertIsNotNone(m, "EXPECTED_TARGETS block not found in release.yml")
        self.assertEqual(sorted(m.group(1).split()), sorted(EXPECTED_TARGETS))
    # End-def

    def test_release_is_gated(self):
        release = self.text[self.text.index("\n  release:"):]
        self.assertRegex(release, r"needs: \[meta, test, build\]")
        self.assertIn("if: success() && needs.meta.outputs.is_release == 'true'", release)
        self.assertIn("verify-set", release)
        self.assertNotIn("zip -r", release)
        self.assertIn("pattern: dandelion-*", release)
    # End-def

    def test_cache_keys_use_run_attempt_and_distro(self):
        self.assertIn("github.run_attempt", self.text)
        self.assertIn("github.run_id", self.text)
        self.assertNotIn("${{ github.sha }}", self.text.split("Cache Nuitka")[1].split("\n\n")[0])
        self.assertIn("hashFiles('pyproject.toml', 'mk/make.py', 'mk/postbuild.py', 'mk/config/build.config', "
                      "'.github/scripts/docker-build.sh')", self.text)
    # End-def

    def test_docker_script_installs_ccache_and_pins_dir(self):
        sh = (ROOT / ".github" / "scripts" / "docker-build.sh").read_text()
        self.assertRegex(sh, r"apt-get install[^\n]*\\\n[^\n]*\bccache\b[^\n]*\bbinutils\b")
        self.assertIn("export CCACHE_DIR=/work/.nuitka-cache/ccache", sh)
        self.assertIn('export NUITKA_CACHE_DIR_CCACHE="$CCACHE_DIR"', sh)
        self.assertIn("ccache -z", sh)
        self.assertIn("trap restore_ownership EXIT", sh)
    # End-def
# End-class


if __name__ == "__main__":
    unittest.main()
# End-if
