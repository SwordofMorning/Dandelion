##
 # @file test/excel_contract/record_golden.py
 # @brief One-shot recording from the pinned, unmodified legacy source only.
 #

import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test"))

from excel_contract.cases import CASES, run_case

LEGACY_COMMIT = "1ce7ac29bef2db46551098e629db50926ae5e96f"
GOLDEN = Path(__file__).with_name("golden.json")


##
 # @brief Never derive expectations from the replacement implementation.
 #
def main():
    if GOLDEN.exists():
        raise SystemExit("Refusing to overwrite frozen golden.json.")
    # End-if
    try:
        import pandas
        import numpy
        import openpyxl
        import tabulate
    except ImportError as error:
        raise SystemExit(f"Legacy recording requires pandas and its dependencies: {error}")
    # End-try

    workspace = subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], cwd=ROOT, text=True
    ).strip()
    legacy_source = subprocess.check_output(
        ["git", "-C", workspace, "show", f"{LEGACY_COMMIT}:src/tool/editor/excel_tool.py"]
    )
    scratch = ROOT / ".playground"
    scratch.mkdir(exist_ok=True)
    results = {}
    with tempfile.TemporaryDirectory(prefix="excel-legacy-", dir=scratch) as directory:
        source_path = Path(directory) / "legacy_excel_tool.py"
        source_path.write_bytes(legacy_source)
        import src.tool.editor
        name = "src.tool.editor._legacy_excel_tool"
        spec = importlib.util.spec_from_file_location(name, source_path)
        module = importlib.util.module_from_spec(spec)
        module.__package__ = "src.tool.editor"
        sys.modules[name] = module
        spec.loader.exec_module(module)
        tools = {"read_excel": module.ReadExcelTool, "write_excel": module.WriteExcelTool}
        for case in CASES:
            case_directory = Path(directory) / case["id"]
            case_directory.mkdir()
            results[case["id"]] = run_case(case, case_directory, tools)
        # End-for
        del sys.modules[name]
    # End-with

    record = {
        "provenance": {
            "legacy_commit": LEGACY_COMMIT,
            "legacy_source_sha256": hashlib.sha256(legacy_source).hexdigest(),
            "pandas": pandas.__version__,
            "numpy": numpy.__version__,
            "openpyxl": openpyxl.__version__,
            "tabulate": tabulate.__version__,
            "python": platform.python_version(),
            "generation_date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        },
        "cases": results,
    }
    with GOLDEN.open("x", encoding="ascii") as output:
        json.dump(record, output, ensure_ascii=True, indent=1, sort_keys=True)
        output.write("\n")
    # End-with
    print(f"Recorded {len(results)} legacy cases to {GOLDEN.relative_to(ROOT)}")
    print(json.dumps(record["provenance"], indent=1, sort_keys=True))
    return 0
# End-def


if __name__ == "__main__":
    sys.exit(main())
# End-if
