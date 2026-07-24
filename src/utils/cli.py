# src/utils/cli.py

import sys
import os
import shlex

class InteractiveCLI:
    def __init__(self, agent_instance, session_manager):
        self.agent = agent_instance
        self.session = session_manager
        self.staged_message = ""

    def _print_help(self):
        print("\n================= REGENT CLI MENU =================")
        print(" -h, --help       : Show this help message.")
        print(" -e, --edit <txt> : Stage text to message buffer.")
        print(" -s, --send       : Send staged message to LLM.")
        print("     --file <pth> : Send content of a file directly.")
        print(" -l, --list       : List [sessions | staged].")
        print(" -c, --change <id>: Switch to a different session.")
        print(" -q, --quit       : Exit the CLI.")
        print("===================================================\n")

    def _handle_list(self, target):
        if target == "sessions":
            sessions = self.session.list_sessions()
            print("\n[+] Available Sessions:")
            for s in sessions:
                mark = "*" if s["id"] == self.session.current_session_id else " "
                print(f" {mark} {s['id']} | {s['name']} | {s['created_at']}")
        elif target == "staged":
            print("\n[+] Currently Staged Message:")
            print("---------------------------------")
            print(self.staged_message if self.staged_message else "(Empty)")
            print("---------------------------------")
        else:
            print("[-] Unknown list target. Use 'sessions' or 'staged'.")

    def _handle_change_session(self, session_id):
        if self.session.switch_session(session_id):
            self.agent.reload_history()
            print(f"[+] Switched to session: {session_id}")
        else:
            print(f"[-] Session {session_id} not found.")

    def run(self):
        print("\n================ SYSTEM READY ================")
        self._print_help()
        
        while True:
            try:
                meta = self.session.get_current_meta()
                sess_name = meta.get("name", "unknown")
                
                # Check if we need to let Agent process tool results
                if self.agent.history and self.agent.history[-1]["role"] == "user":
                    content = self.agent.history[-1]["content"]
                    if isinstance(content, list) and len(content) > 0 and content[0].get("type") == "tool_result":
                        print("\n[*] Processing pending tool results...")
                        while self.agent.step():
                            pass
                        continue

                cmd_input = input(f"\n[Regent | {sess_name}]> ").strip()
                if not cmd_input:
                    continue
                    
                try:
                    parts = shlex.split(cmd_input)
                except ValueError as e:
                    print(f"[-] Input parsing error: {e}")
                    continue
                    
                cmd = parts[0]
                
                if cmd in ['-h', '--help']:
                    self._print_help()
                    
                elif cmd in ['-q', '--quit']:
                    print("[*] Exiting cleanly.")
                    break
                    
                elif cmd in ['-e', '--edit']:
                    if len(parts) > 1:
                        text = " ".join(parts[1:])
                        self.staged_message += text + "\n"
                        print("[+] Text added to stage buffer.")
                    else:
                        print("[-] Please provide text to edit. Example: -e \"hello world\"")
                        
                elif cmd in ['-l', '--list']:
                    target = parts[1] if len(parts) > 1 else "sessions"
                    self._handle_list(target)
                    
                elif cmd in ['-c', '--change']:
                    if len(parts) > 1:
                        self._handle_change_session(parts[1])
                    else:
                        print("[-] Please provide a session ID. Example: -c sess_2024...")
                        
                elif cmd in ['-s', '--send']:
                    final_msg = ""
                    if len(parts) > 2 and parts[1] == '--file':
                        filepath = parts[2]
                        if os.path.exists(filepath):
                            with open(filepath, "r", encoding="utf-8") as f:
                                final_msg = f.read()
                        else:
                            print(f"[-] File not found: {filepath}")
                            continue
                    else:
                        final_msg = self.staged_message.strip()
                        
                    if not final_msg:
                        print("[-] Nothing to send. Stage message first with -e or use --file.")
                        continue
                        
                    print("\n--- Message to Send ---")
                    print(final_msg[:500] + ("...\n[Truncated]" if len(final_msg) > 500 else ""))
                    print("-----------------------")
                    
                    ans = input("Proceed to send? [y/N]: ").strip().lower()
                    if ans in ['y', 'yes']:
                        self.agent.inject_user_message(final_msg)
                        self.staged_message = "" # Clear buffer
                        print("[*] Running LLM Inference...")
                        while self.agent.step():
                            pass
                    else:
                        print("[-] Cancelled send.")
                        
                else:
                    print(f"[-] Unknown command: {cmd}. Type -h for help.")
                    
            except KeyboardInterrupt:
                print("\n[!] Use '-q' to exit safely!")