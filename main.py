##
 # @file main.py
 # @date 2026/08/04
 # 
 # @brief Main function entrance.
 #

import os
import sys

from src.utils import load_api_config
from src.utils import SessionManager
from src.utils import InteractiveCLI
from src.core.agent import MyAgent

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def main():
    print("[>] Initializing Regent Project...")

    # 1. Load Configurations
    cfg_path = os.path.join(BASE_DIR, ".env", "api.cfg")
    config = load_api_config(cfg_path)
    if not config:
        print(f"[-] FATAL: Failed to load config at {cfg_path}.")
        sys.exit(1)

    # 2. Init Session Manager
    session_mgr = SessionManager(log_dir=os.path.join(BASE_DIR, ".log"))

    # 3. Init Agent
    agent = MyAgent(
        config=config, 
        session_manager=session_mgr, 
        workspace_dir=BASE_DIR
    )

    print(f"[+] Agent Initialization Successful. Model: {config['MODEL_ID']}")

    # 4. Start CLI
    cli = InteractiveCLI(agent_instance=agent, session_manager=session_mgr)
    cli.run()

if __name__ == "__main__":
    main()