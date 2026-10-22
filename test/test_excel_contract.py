##
 # @file test/test_excel_contract.py
 # @brief Frozen legacy behavior, dependency isolation, and workbook lifetime tests.
 #

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test"))

from excel_contract.cases import BASIC_TABLE, CASES, run_case, workbook_builder
from src.tool.editor import excel_tool

GOLDEN_PATH = ROOT / "test" / "excel_contract" / "golden.json"
GOLDEN_SHA256 = "2029380f56e35093db11de6e0fc85379a06ca37be80aefad6350d52d4c663e5a"
GOLDEN = json.loads(GOLDEN_PATH.read_text(encoding="ascii"))
TOOLS = {"read_excel": excel_tool.ReadExcelTool, "write_excel": excel_tool.WriteExcelTool}

# Only these independently reviewed results may differ from the frozen legacy record.
APPROVED_DIFFS = {
    "read_sheet_none": {
        "success": False,
        "text": "Error: 'sheet' must be a sheet name or 0-based index; reading all sheets is not supported.",
        "reason": "Replace the legacy uncaught AttributeError on an all-sheets dict with a clear error.",
    },
}


##
 # @brief Keep all generated files inside the supplied project directory.
 #
def sandbox():
    scratch = ROOT / ".playground"
    scratch.mkdir(exist_ok=True)
    return tempfile.TemporaryDirectory(prefix="excel-contract-", dir=scratch)
# End-def


##
 # @brief Exact per-case comparison, including saved cell types and header styling.
 #
class ExcelGoldenContract(unittest.TestCase):
    def test_frozen_provenance(self):
        self.assertEqual(hashlib.sha256(GOLDEN_PATH.read_bytes()).hexdigest(), GOLDEN_SHA256)
        self.assertEqual(GOLDEN["provenance"]["legacy_commit"], "1ce7ac29bef2db46551098e629db50926ae5e96f")
        self.assertEqual(GOLDEN["provenance"]["legacy_source_sha256"], "5ddbf8963b01908f5b4ea929b7e3b3291adb9794689717c528bd918ef01609a2")
        self.assertEqual(set(GOLDEN["cases"]), {case["id"] for case in CASES})
        self.assertEqual(len(CASES), len({case["id"] for case in CASES}))
        self.assertTrue(set(APPROVED_DIFFS) <= set(GOLDEN["cases"]))
        for difference in APPROVED_DIFFS.values():
            self.assertTrue(difference["reason"])
        # End-for
    # End-def

    def check_case(self, case):
        with sandbox() as directory:
            actual = run_case(case, directory, TOOLS)
        # End-with
        expected = GOLDEN["cases"][case["id"]]
        if case["id"] in APPROVED_DIFFS:
            difference = APPROVED_DIFFS[case["id"]]
            self.assertNotEqual(actual, expected, "Remove stale approved differences")
            self.assertEqual(actual["success"], difference["success"])
            if "text_prefix" in difference:
                self.assertTrue(actual["text"].startswith(difference["text_prefix"]))
            else:
                self.assertEqual(actual["text"], difference["text"])
            # End-if
            if "workbook" in expected:
                self.assertEqual(actual["workbook"], expected["workbook"])
            # End-if
        else:
            self.assertEqual(actual, expected)
        # End-if
    # End-def
# End-class


##
 # @brief Each fixture gets a separately named unittest for useful failure output.
 #
def fixture_test(case):
    def test(self):
        self.check_case(case)
    # End-def
    return test
# End-def


for _case in CASES:
    setattr(ExcelGoldenContract, "test_" + _case["id"], fixture_test(_case))
# End-for


##
 # @brief Verify tools in a fresh interpreter, including installed optional packages.
 #
class ExcelDependencyContract(unittest.TestCase):
    def run_clean_interpreter(self, block_optional):
        script = '''
import importlib.abc
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
class BlockOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.partition(".")[0] in ("pandas", "numpy"):
            raise ImportError("Test forbids " + fullname)
        return None
if sys.argv[3] == "blocked":
    sys.meta_path.insert(0, BlockOptional())
from src.tool.editor.excel_tool import ReadExcelTool, WriteExcelTool
workspace = sys.argv[2]
assert "pandas" not in sys.modules
success, text = WriteExcelTool(workspace).execute(
    file_path="output.xlsx", markdown_table=sys.argv[4])
assert success, text
success, text = ReadExcelTool(workspace).execute(file_path="output.xlsx")
assert success and "2 rows x 2 cols" in text and "apple" in text, text
forbidden = ("pandas", "numpy") if sys.argv[3] == "blocked" else ("pandas",)
assert not any(name.partition(".")[0] in forbidden for name in sys.modules)
print("read/write succeeded without pandas or numpy")
'''
        with sandbox() as directory:
            result = subprocess.run(
                [sys.executable, "-c", script, str(ROOT), directory,
                 "blocked" if block_optional else "clean", BASIC_TABLE],
                cwd=directory, capture_output=True, text=True, timeout=60,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        # End-with
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("read/write succeeded without pandas or numpy", result.stdout)
    # End-def

    # openpyxl may probe an installed numpy as an optional extra, so the clean
    # run only forbids pandas; the blocked run proves numpy is not required.
    def test_clean_interpreter_never_imports_pandas(self):
        self.run_clean_interpreter(False)
    # End-def

    def test_array_imports_forbidden(self):
        self.run_clean_interpreter(True)
    # End-def

    def test_static_no_pandas_numpy_imports_in_src(self):
        import ast
        offenders = []
        for path in sorted((ROOT / "src").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    names = [node.module]
                # End-if
                for name in names:
                    if name.partition(".")[0] in ("pandas", "numpy"):
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: {name}")
                    # End-if
                # End-for
            # End-for
        # End-for
        self.assertEqual(offenders, [])
    # End-def

    def test_dependency_hint(self):
        for tool in TOOLS.values():
            with self.subTest(tool=tool.__name__):
                with mock.patch.dict(sys.modules, {"openpyxl": None}):
                    success, text = tool(str(ROOT)).execute()
                # End-with
                self.assertFalse(success)
                self.assertTrue(text.startswith("Error: Missing libraries ("))
                self.assertTrue(text.endswith("Run: pip install openpyxl tabulate"))
            # End-with
        # End-for
    # End-def
# End-class


##
 # @brief A proxy records close() while preserving real read-only worksheet behavior.
 #
class TrackedWorkbook:
    def __init__(self, workbook):
        self.workbook = workbook
        self.close_calls = 0
    # End-def

    def __getattr__(self, name):
        return getattr(self.workbook, name)
    # End-def

    def __getitem__(self, name):
        return self.workbook[name]
    # End-def

    def close(self):
        self.close_calls += 1
        self.workbook.close()
    # End-def
# End-class


##
 # @brief Workbook handles must close on early returns and both error categories.
 #
class ExcelWorkbookLifetime(unittest.TestCase):
    def check_closed(self, rows, kwargs=None, failure=None):
        openpyxl, _ = excel_tool._load_libraries()
        load_workbook = openpyxl.load_workbook
        opened = []

        def load(*args, **options):
            self.assertTrue(options["read_only"])
            self.assertTrue(options["data_only"])
            tracked = TrackedWorkbook(load_workbook(*args, **options))
            opened.append(tracked)
            return tracked
        # End-def

        with sandbox() as directory:
            workbook_builder(rows)(directory)
            with mock.patch.object(openpyxl, "load_workbook", side_effect=load):
                if failure is None:
                    result = excel_tool.ReadExcelTool(directory).execute(file_path="input.xlsx", **(kwargs or {}))
                else:
                    with mock.patch.object(excel_tool, failure, side_effect=RuntimeError("injected failure")):
                        result = excel_tool.ReadExcelTool(directory).execute(file_path="input.xlsx")
                    # End-with
                # End-if
            # End-with
            self.assertEqual(len(opened), 1)
            self.assertEqual(opened[0].close_calls, 1)
            self.assertIsNone(opened[0].workbook._archive.fp)
        # End-with
        return result
    # End-def

    def test_close_success(self):
        self.assertTrue(self.check_closed([["a"], [1]])[0])
    # End-def

    def test_close_empty_worksheet(self):
        self.assertEqual(self.check_closed([]), (True, "(Empty table: no data rows found)"))
    # End-def

    def test_close_header_only(self):
        self.assertEqual(self.check_closed([["a"]]), (True, "(Empty table: no data rows found)"))
    # End-def

    def test_close_read_exception(self):
        self.assertEqual(self.check_closed([["a"], [1]], failure="_sheet_rows"),
                         (False, "Error reading Excel: injected failure"))
    # End-def

    def test_close_format_exception(self):
        self.assertEqual(self.check_closed([["a"], [1]], failure="_table_rows"),
                         (False, "Error formatting table: injected failure"))
    # End-def

    def test_close_invalid_sheet(self):
        self.assertFalse(self.check_closed([["a"], [1]], kwargs={"sheet": "Missing"})[0])
    # End-def
# End-class


if __name__ == "__main__":
    unittest.main()
# End-if
