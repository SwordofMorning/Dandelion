##
 # @file main.py
 # @date 2026/08/07
 # 
 # @brief Main function entrance.
 #
 # @usage
 #   dandelion              : start the interactive agent
 #   dandelion --version    : print "v<version>" and exit 0 (no config, no network)
 #   dandelion --self-test  : offline check of the bundled runtime, exit 0/1
 #   dandelion --help       : print usage
 #
 # @note Heavy project imports are deferred into main() so that --version and
 #   --self-test run before load_api_config()/SessionManager() and still work
 #   when a dependency is broken (the self-test then reports it). Nuitka follows
 #   function-level imports, so the compiled artifact content is unchanged.
 #

import sys

# Must stay the first project import: it appends bin/_stdlib_fallback to
# sys.path before any bypassed package triggers an on-disk stdlib import.
from mk.lib.paths import base_dir, config_path, log_dir, is_compiled

# Build metadata
try:
    from mk.lib import build_info as _build_info
except ImportError:
    _build_info = None
# End-try

USAGE = """Usage: dandelion [--version | --self-test | --help]

  (no option)   Start the interactive agent.
  --version     Print the version and exit.
  --self-test   Run offline runtime checks (imports, TLS, crypto, Excel) and exit.
  --help        Show this help.
"""

##
 # @brief Dotted stdlib modules imported by the bypassed (copied, not compiled)
 #   packages. In the artifact these may live in a stdlib package that Nuitka
 #   compiled only partially, so each one is imported by --self-test.
 #
 # Kept in sync with the installed dependencies by test/test_build_contract.py
 # (tkinter.* is intentionally excluded: optional tqdm GUI, never used).
 #
SELF_TEST_STDLIB_SUBMODULES = (
    "asyncio.base_events", "asyncio.coroutines", "collections.abc",
    "concurrent.futures", "email.errors", "email.message", "email.parser",
    "email.utils", "encodings.aliases", "encodings.idna", "http.client",
    "http.cookiejar", "http.cookies", "importlib.metadata",
    "importlib.resources", "urllib.parse", "urllib.request",
    "xml.dom.minidom", "xml.etree.ElementTree",
)

##
 # @brief Version string without the leading "v".
 #
def _version():
    return _build_info.VERSION if _build_info else "dev"
# End-def

##
 # @brief Offline self-test checks: (name, callable returning a detail string).
 #
 # Every check is REQUIRED: any exception makes --self-test exit non-zero.
 # Nothing here reads .env/api.cfg, creates a session or touches the network.
 #
def _self_test_checks():
    def stdlib_native():
        import ctypes, hashlib, sqlite3, zlib, json, zoneinfo  # noqa: F401
        conn = sqlite3.connect(":memory:")
        try:
            value = conn.execute("select 40 + 2").fetchone()[0]
        finally:
            conn.close()
        # End-try
        if value != 42:
            raise RuntimeError("sqlite3 returned %r" % (value,))
        # End-if
        hashlib.sha256(zlib.compress(b"dandelion")).hexdigest()
        ctypes.sizeof(ctypes.c_void_p)
        return "sqlite3 %s, ctypes, zlib, hashlib" % sqlite3.sqlite_version
    # End-def

    def stdlib_submodules():
        import importlib
        missing = []
        for name in SELF_TEST_STDLIB_SUBMODULES:
            try:
                importlib.import_module(name)
            except ImportError as e:
                missing.append("%s (%s)" % (name, e))
            # End-try
        # End-for
        if missing:
            raise ImportError("missing stdlib submodules: " + "; ".join(missing))
        # End-if
        import xml.etree.ElementTree as ET
        if ET.fromstring("<a><b>1</b></a>").find("b").text != "1":
            raise RuntimeError("xml.etree.ElementTree parse mismatch")
        # End-if
        return "%d submodules, ElementTree parse" % len(SELF_TEST_STDLIB_SUBMODULES)
    # End-def

    def tls():
        import ssl
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        count = len(ctx.get_ca_certs())
        if count == 0:
            raise RuntimeError("certifi bundle loaded 0 CA certificates")
        # End-if
        return "%s, %d CA certs" % (ssl.OPENSSL_VERSION, count)
    # End-def

    def config_libs():
        import yaml
        import prompt_toolkit
        from prompt_toolkit import PromptSession  # noqa: F401
        if yaml.safe_load("a: 1") != {"a": 1}:
            raise RuntimeError("yaml.safe_load mismatch")
        # End-if
        return "pyyaml %s, prompt_toolkit %s" % (yaml.__version__, prompt_toolkit.__version__)
    # End-def

    def http_stack():
        import requests
        import httpx
        import pydantic
        session = requests.Session()
        session.close()
        return "requests %s, httpx %s, pydantic %s" % (
            requests.__version__, httpx.__version__, pydantic.VERSION)
    # End-def

    def llm_sdks():
        import anthropic
        import openai
        # Client construction is local only (no request is sent).
        anthropic.Anthropic(api_key="self-test", max_retries=0).close()
        openai.OpenAI(api_key="self-test", max_retries=0).close()
        return "anthropic %s, openai %s" % (anthropic.__version__, openai.__version__)
    # End-def

    def ssh_crypto():
        import paramiko
        from cryptography.hazmat.primitives.asymmetric import ed25519
        key = ed25519.Ed25519PrivateKey.generate()
        sig = key.sign(b"dandelion")
        key.public_key().verify(sig, b"dandelion")
        return "paramiko %s, ed25519 sign/verify" % paramiko.__version__
    # End-def

    def excel_roundtrip():
        import tempfile
        from src.tool.editor.excel_tool import ReadExcelTool, WriteExcelTool
        with tempfile.TemporaryDirectory(prefix="dandelion-selftest-") as tmp:
            ok, msg = WriteExcelTool(workspace_dir=tmp).execute(
                file_path="self_test.xlsx",
                markdown_table="| id | name |\n| --- | --- |\n| 001 | alpha |\n| 002 | beta |")
            if not ok:
                raise RuntimeError(msg)
            # End-if
            ok, msg = ReadExcelTool(workspace_dir=tmp).execute(file_path="self_test.xlsx")
            if not ok or "Shape: 2 rows x 2 cols" not in msg or "alpha" not in msg:
                raise RuntimeError("unexpected read result: %r" % (msg,))
            # End-if
        # End-with
        return "openpyxl/tabulate write+read"
    # End-def

    def project_modules():
        import src.utils  # noqa: F401
        import src.tool  # noqa: F401
        import src.core.agent  # noqa: F401
        return "src.utils, src.tool, src.core.agent"
    # End-def

    def no_heavy_deps():
        # Phase B removed pandas/numpy; make a regression visible in the artifact.
        import importlib.util
        found = [m for m in ("pandas", "numpy") if importlib.util.find_spec(m) is not None]
        if found and is_compiled():
            raise RuntimeError("unexpected bundled packages: " + ", ".join(found))
        # End-if
        return "absent" if not found else "present in dev env (ignored): " + ", ".join(found)
    # End-def

    return [
        ("stdlib-native", stdlib_native),
        ("stdlib-submodules", stdlib_submodules),
        ("tls-certifi", tls),
        ("yaml-prompt_toolkit", config_libs),
        ("http-stack", http_stack),
        ("llm-sdks", llm_sdks),
        ("ssh-crypto", ssh_crypto),
        ("excel-roundtrip", excel_roundtrip),
        ("project-modules", project_modules),
        ("no-pandas-numpy", no_heavy_deps),
    ]
# End-def

##
 # @brief Run the offline self-test.
 #
 # @return Process exit code (0 = all checks passed).
 #
def run_self_test():
    import platform
    import traceback

    print("[>] Dandelion self-test v%s" % _version())
    print("    platform : %s %s (%s)" % (platform.system(), platform.machine(),
                                       _build_info.PLATFORM if _build_info else "source"))
    print("    python   : %s" % sys.version.split()[0])
    print("    compiled : %s" % is_compiled())
    print("    base_dir : %s" % base_dir())

    failures = 0
    checks = _self_test_checks()
    for name, func in checks:
        try:
            detail = func()
            print("[+] %-20s ok   %s" % (name, detail))
        except BaseException as e:  # noqa: B902 - a SystemExit in a check is a failure too
            failures += 1
            print("[-] %-20s FAIL %s: %s" % (name, type(e).__name__, e))
            traceback.print_exc(limit=3, file=sys.stdout)
        # End-try
    # End-for

    if failures:
        print("Self-test FAILED (%d of %d checks)" % (failures, len(checks)))
        return 1
    # End-if
    print("Self-test OK")
    return 0
# End-def

##
 # @brief Start the interactive agent (original startup path).
 #
def run_agent():
    from src.utils import load_api_config
    from src.utils import SessionManager
    from src.utils import InteractiveCLI
    from src.core.agent import MyAgent

    print(f"[>] Initializing Dandelion Project (v{_version()})...")

    # 1. Load Configurations
    cfg_path = config_path()
    config = load_api_config(cfg_path)
    if not config:
        print(f"[-] FATAL: Failed to load config at {cfg_path}.")
        sys.exit(1)
    # End-if

    # 2. Init Session Manager
    session_mgr = SessionManager(log_dir=log_dir())

    # 3. Init Agent
    agent = MyAgent(
        config=config,
        session_manager=session_mgr,
        workspace_dir=base_dir()
    )

    print(f"[+] Agent Initialization Successful. Model: {config.get('NAME', config['MODEL_ID'])}")
    print(f"[+] MAX_TOKENS={config['MAX_TOKENS']}, MAX_CONTEXT_TOKENS={config['MAX_CONTEXT_TOKENS']}")

    # 4. Start CLI
    cli = InteractiveCLI(agent_instance=agent, session_manager=session_mgr)
    cli.run()
    return 0
# End-def

##
 # @brief Entry point: dispatch CLI flags, otherwise start the agent.
 #
 # @param argv Argument list without the program name (default: sys.argv[1:]).
 #
 # @return Process exit code.
 #
def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if not args:
        return run_agent()
    # End-if
    if len(args) == 1 and args[0] in ("--version", "-V"):
        print("v" + _version())
        return 0
    # End-if
    if len(args) == 1 and args[0] == "--self-test":
        return run_self_test()
    # End-if
    if len(args) == 1 and args[0] in ("--help", "-h"):
        print(USAGE)
        return 0
    # End-if
    print("[-] Unknown arguments: " + " ".join(args))
    print(USAGE)
    return 2
# End-def

if __name__ == "__main__":
    sys.exit(main())
# End-if
