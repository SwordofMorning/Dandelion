##
 # @file main.py
 # @date 2026/08/07
 #
 # @brief Dandelion CLI and Remote application entry point.
 # Dispatches `python main.py remote ...` to the Dandelion Remote lifecycle
 # manager, handles top-level `-h`/`--help` and `-v`/`--version` flags, and
 # otherwise loads local CLI configuration and starts the interactive shell.
 #

import sys

from mk.lib.paths import (
    base_dir,
    config_dir,
    config_path,
    log_dir,
    protected_plugin_dirs,
    tool_dirs,
)

# Build metadata
try:
    from mk.lib import build_info as _build_info
except ImportError:
    _build_info = None
# End-try


##
 # @brief Initialize the shared Core and run the terminal frontend, or dispatch
 # `python main.py remote ...` to the Dandelion Remote lifecycle manager.
 #
 # @param argv Optional command arguments; None reads sys.argv[1:].
 #
 # @return None.
 #
def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "remote":
        try:
            from src.remote.service import main as remote_main
        except ImportError:
            print("[-] FATAL: Dandelion Remote is server-only and not bundled in standalone CLI builds.")
            sys.exit(1)
        # End-try
        sys.exit(remote_main(args[1:]))
    # End-if

    ver = _build_info.VERSION if _build_info else "dev"
    if args and args[0] in ("-h", "--help"):
        print("Usage: dandelion [remote {init|start|stop|restart|status|logs|fg}]")
        print("       dandelion [-h|--help] [-v|--version]")
        return
    # End-if
    if args and args[0] in ("-v", "--version"):
        print(f"dandelion {ver}")
        return
    # End-if

    from dandelion_core.agent import MyAgent
    from dandelion_core.utils.config import load_api_config
    from dandelion_core.utils.logging import SessionManager
    from src.cli import CLIPromptBuilder, InteractiveCLI
    from src.cli.runtime import cli_runtime

    print(f"[>] Initializing Dandelion Project (v{ver})...")

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
    extra_tool_dirs = config.get("TOOL_DIRS")
    agent = MyAgent(
        config=config,
        session_manager=session_mgr,
        workspace_dir=base_dir(),
        runtime=cli_runtime(),
        prompt_builder_cls=CLIPromptBuilder,
        config_dir=config_dir(),
        host="cli",
        tool_dirs=tool_dirs("cli", extra_tool_dirs),
        protected_dirs=protected_plugin_dirs(extra_tool_dirs),
    )

    print(f"[+] Agent Initialization Successful. Model: {config.get('NAME', config['MODEL_ID'])}")
    print(f"[+] MAX_TOKENS={config['MAX_TOKENS']}, MAX_CONTEXT_TOKENS={config['MAX_CONTEXT_TOKENS']}")

    # 4. Start CLI
    cli = InteractiveCLI(agent_instance=agent, session_manager=session_mgr)
    cli.run()
# End-def


if __name__ == "__main__":
    main()
# End-if
