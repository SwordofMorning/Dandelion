##
 # @file src/utils/cli/cli.py
 # @date 2026/08/04
 # 
 # @brief Interactive CLI for Dandelion.
 #
 # @note Ctrl+C contract (turn interrupt + rollback):
 #   While a turn runs the SIGINT handler only records a stop request; the
 #   in-flight step finishes, then a checkpoint consumes the request, restores
 #   the session state captured before the commit (see SessionBackup) and hands
 #   the original input back to the staged buffer. If the turn has no usable
 #   backup the request is refused and the agent keeps running, so a turn is
 #   never left with a tool_use and no matching tool_result.
 #

import os
import shlex
import signal
import subprocess
import builtins

from .interrupt import (
    request as request_stop,
    is_requested as stop_requested,
    reason as stop_reason,
    clear as clear_stop,
)
from ..logging.backup import SessionBackup

##
 # @note import toolkit
 # if success, there will be Tab auto-completion
 # if not, could still use without auto-completion.
 #
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import NestedCompleter, PathCompleter
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.formatted_text import ANSI
    HAS_PTK = True
except ImportError:
    HAS_PTK = False

from .printer import CLIPrinter

##
 # @brief Interactive CLI for Dandelion workspace management.
 #
class InteractiveCLI:
    ##
     # @brief Constructor.
     # 
     # @param agent_instance instance of class MyAgent.
     # @param session_manager instance of SessionManager.
     # 
    def __init__(self, agent_instance, session_manager):
        # Assignment object.
        self.agent = agent_instance
        self.session = session_manager
        # Init printer.
        self.cli = CLIPrinter()

        # @note The staged area is persisted per-session
        #       (.log/sess_xx/staged.md), so `checkout` / `exit` never lose an
        #       edited draft. It is loaded at startup / session switch.
        self.staged_message = self.session.load_staged()
        if self.staged_message.strip():
            self.cli.info(f"Restored pending draft ({len(self.staged_message.strip())} chars) from this branch.")

        # ----- @par Interrupt / rollback runtime flags -----
        # @note _turn_active  : a turn is running, so SIGINT is ours to record.
        #       _backup_ready : this turn has a complete backup and may be
        #                       rolled back (reset on rollback / checkout).
        self._turn_active = False
        self._backup_ready = False

        # Initialize prompt_toolkit session with in-memory history
        if HAS_PTK:
            self.prompt_session = PromptSession(history=InMemoryHistory())
        else:
            self.prompt_session = None
        # End-if
    # End-def

    ##
     # @brief Dynamically build the context-aware completer before each prompt.
     #
     # @return comp_dict or None (if not HAS_PTK).
     #
    def _build_completer(self):
        if not HAS_PTK:
            return None

        # Gather session's info, used for `checkout` and `branch`.
        sessions = self.session.list_sessions()
        session_targets = {}
        for s in sessions:
            session_targets[s['name']] = None
            session_targets[s['id']] = None
        # End-for

        # Completion Dict.
        comp_dict = {
            'branch': {
                '-a': None,
                '-d': session_targets
            },
            'checkout': {
                **session_targets,
                '-b': None
            },
            'vim': None,
            'load': PathCompleter(expanduser=True),
            'status': None,
            'commit': None,
            'clear': None,
            'help': None,
            'quit': None,
            'exit': None
        }

        return NestedCompleter.from_nested_dict(comp_dict)
    # End-def

    ##
     # @brief Help.
     #
    def _print_help(self):
        help_text = (
            f"{self.cli.C_CYAN}\n================= WORKSPACE ================={self.cli.C_RESET}\n"
            " Git-Style Session Management:\n"
            "   branch -a             : List all available sessions.\n"
            "   branch -d <name/id>   : Delete one session.\n"
            "   checkout <name/id>    : Switch to an existing session.\n"
            "   checkout -b <name>    : Create and switch to a new session.\n\n"
            " Vim-Style Editing:\n"
            "   vim                   : Open system editor (Vim/Notepad) to draft prompt.\n"
            "   load <filepath>       : Load a local file into the prompt buffer.\n\n"
            " Core Operations:\n"
            "   status                : View current session and staged buffer.\n"
            "   commit                : Send the staged buffer to LLM.\n"
            "   clear                 : Clear the staged buffer.\n"
            "   help / quit / exit    : System commands.\n"
            f"{self.cli.C_CYAN}===================================================={self.cli.C_RESET}\n"
        )
        self.cli.raw(help_text)
    # End-def

    ##
     # @brief Map session names to exact session IDs.
     #
     # @param target Session name or ID.
     # 
     # @return ID or None.
     # 
     # @retval id Session's ID.
     # @retval None No such id or name.
     #
    def _resolve_session_id(self, target):
        sessions = self.session.list_sessions()
        for s in sessions:
            if target == s['id'] or target == s['name']:
                return s['id']
        # End-for
        return None
    # End-def

    ##
     # @brief `brach` command handle. 
     # branch -a:            list all branch;
     # branch -d <name/id>:  delete selected branch.
     #
     # @param args Terminal input.
     #
    def _cmd_branch(self, args):
        # ----- 1. List all branch -----
        if not args or args[0] == '-a':
            # Get sessions.
            sessions = self.session.list_sessions()
            self.cli.success("\nAvailable Sessions (Branches):")
            # Traverse and print.
            for s in sessions:
                mark = "*" if s["id"] == self.session.current_session_id else " "
                self.cli.raw(f" {mark} {s['name']:<20} | {s['id']}")
            # End-for
            self.cli.raw("")
        # End-if

        # ----- 2. Delete branch <name/id> -----
        elif args[0] == '-d':
            # No <name/id>.
            if len(args) < 2:
                self.cli.error("Usage: branch -d <name/id>")
                return
            # End-if

            # Get Session's ID.
            target = args[1]
            session_id = self._resolve_session_id(target)

            # Session ID not found.
            if not session_id:
                self.cli.error(f"Error: Session '{target}' not found.")
                return
            # End-if

            # Ask to delete.
            ans = input(f"{self.cli.C_YELLOW}[!]{self.cli.C_RESET} Are you sure you want to delete branch '{target}'? [y/N]: ").strip().lower()
            if ans in ['y', 'yes']:
                success, msg = self.session.delete_session(session_id)
                if success:
                    self.cli.success(msg)
                else:
                    self.cli.error(msg)
            else:
                self.cli.error("Deletion aborted.")
            # End-if
        # End-elif

        # ----- 3. Others -----
        else:
            self.cli.error(f"Unknown branch argument: {args[0]}. Try 'branch -a' or 'branch -d'.")
        # End-else
    # End-def

    ##
     # @brief `checkout` command handle. 
     # checkout -b <name/id>:    create a new session branch;
     # checkout <name/id>:       switch to one existed session branch.
     #
     # @param args Terminal input.
     #
    def _cmd_checkout(self, args):
        # Error
        if not args:
            self.cli.error("Usage: checkout <name> OR checkout -b <new_name>")
            return
        # End-if

        # If the current branch has a pending draft, note that it is preserved:
        # drafts are session-scoped (staged.md), so switching branches never
        # loses content; the buffer simply follows the session.
        if self.staged_message.strip():
            self.cli.info(f"Note: pending draft ({len(self.staged_message.strip())} chars) is preserved in the current branch.")
        # End-if

        # ----- 1. Create new session branch -----
        if args[0] == '-b':
            # Error
            if len(args) < 2:
                self.cli.error("Error: Please provide a name for the new session.")
                return
            # End-if

            # @note Here not check duplicate name;
            # Several session with different ID could have same name.

            # Assignment session name.
            new_name = args[1]
            # Generate session ID
            new_id = self.session.create_session(new_name)
            # Refresh agent's history (nothing).
            self.agent.reload_history()
            # Load the (empty) staged buffer of the new branch.
            self.staged_message = self.session.load_staged()
            # The backup belongs to the session directory: a new branch always
            # starts without rollback capability until its first commit.
            self._backup_ready = False
            # Print success.
            self.cli.success(f"Switched to a new session branch: '{new_name}'")
            return
        # End-if

        # ----- 2. Checkout to existed session branch -----
        target = args[0]
        session_id = self._resolve_session_id(target)

        # Session not found.
        if not session_id:
            self.cli.error(f"Error: Session '{target}' not found.")
            return
        # End-if

        # Try to switch/checkout session
        if self.session.switch_session(session_id):
            self.agent.reload_history()
            # Switch the staged buffer to the target branch's draft.
            self.staged_message = self.session.load_staged()
            # The backup belongs to the session directory, so a switch always
            # invalidates the current turn's rollback capability.
            self._backup_ready = False
            if self.staged_message.strip():
                self.cli.info(f"Restored pending draft ({len(self.staged_message.strip())} chars) in this branch.")
            self.cli.success(f"Switched to session branch: '{target}'")
        else:
            self.cli.error(f"Error: Failed to switch to '{target}'. Directory might be corrupted.")
        # End-if
    # End-def

    ##
     # @brief `vim` command handle.
     # Open editor and write message, saved on staged buffer.
     #
     # @return True on a completed editor session; False when the editor could
     #         not be launched (draft preserved, buffer untouched).
     #
    def _cmd_vim(self):
        # Set default editor: vim on Linux and notepad on Windows.
        editor = os.environ.get('EDITOR')
        if not editor:
            editor = 'vim' if os.name != 'nt' else 'notepad'
        # End-if

        # The draft file IS the persisted staged buffer
        # (.log/sess_xx/staged.md): single source of truth, vim swap recovery
        # lives in the session dir, and no /tmp scratch file is involved.
        staged_file = self.session.get_staged_file()
        if not staged_file:
            self.cli.error("Error: No active session; cannot edit the staged draft.")
            return False
        # End-if

        # Ensure the file exists with the current buffer content.
        self.session.save_staged(self.staged_message)

        # Parse the editor command with shlex to support flags
        editor_cmd = shlex.split(editor, posix=(os.name != 'nt'))
        # Normalize the executable token on Windows to remove surrounding quotes
        if os.name == 'nt' and editor_cmd:
            editor_cmd[0] = editor_cmd[0].strip('"').strip("'")
        # End-if

        editor_cmd.append(staged_file)
        try:
            subprocess.call(editor_cmd)
        except FileNotFoundError:
            # The configured editor could not be launched. The staged draft is
            # preserved (it was written above) and the exception must NOT
            # propagate to run()'s generic error handler.
            self.cli.error(f"Error: Editor '{editor_cmd[0]}' could not be launched.")
            self.cli.info("Staged draft preserved. Check your EDITOR setting.")
            return False
        # End-try

        # Read back user input (the editor wrote the file in place).
        new_content = self.session.load_staged()
        if new_content != self.staged_message:
            self.staged_message = new_content
            self.cli.success("Buffer successfully updated via editor.")
        else:
            self.cli.info("Buffer unchanged.")
        return True
    # End-def

    ##
     # @brief `load` command handle. 
     # load <filepath>: load a file (like .md) to staged message buffer.
     #
     # @param args Terminal input.
     #
    def _cmd_load(self, args):
        # Error
        if not args:
            self.cli.error("Usage: load <filepath>")
            return
        # End-if

        # Error
        filepath = args[0]
        if not os.path.exists(filepath):
            self.cli.error(f"Error: File not found -> {filepath}")
            return
        # End-if

        # Overwrite staged message buffer which is not empty.
        if self.staged_message.strip():
            ans = input(f"{self.cli.C_YELLOW}[!]{self.cli.C_RESET} Warning: The buffer is not empty. Overwrite? [y/N]: ").strip().lower()
            if ans not in ['y', 'yes']:
                self.cli.error("Load aborted.")
                return
            # End-if
        # End-if

        # Write-in
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                self.staged_message = f.read().strip()
            # Persist the buffer to the session-scoped staged file.
            self.session.save_staged(self.staged_message)
            self.cli.success(f"Successfully loaded {os.path.getsize(filepath)} bytes into buffer.")
        except Exception as e:
            self.cli.error(f"Error loading file: {e}")
    # End-def

    ##
     # @brief `clear` command handle.
     # Clear the staged buffer (memory + persisted staged.md).
     #
    def _cmd_clear(self):
        self.staged_message = ""
        self.session.clear_staged()
        self.cli.success("Buffer cleared.")
    # End-def

    ##
     # @brief `status` command handle. 
     # Show current branch and staged message buffer.
     #
    def _cmd_status(self):
        # Print branch info.
        meta = self.session.get_current_meta()
        self.cli.info(f"\nCurrent Branch : {meta.get('name', 'Unknown')}")
        self.cli.info(f"History Turns  : {len(self.agent.history)}")
        self.cli.info(f"Staged File    : {self.session.get_staged_file()}")

        # No staged message.
        if not self.staged_message:
            self.cli.info("Staged Buffer  : (Empty)\n")
            return
        # End-if

        # Print stated message.
        self.cli.info("Staged Buffer Preview:")
        self.cli.raw("-" * 50)
        preview = self.staged_message[:300]
        self.cli.raw(preview)
        # Cut-off
        if len(self.staged_message) > 300:
            self.cli.raw("\n... [Truncated]")
        self.cli.raw("-" * 50)
        self.cli.raw(f"    (Total: {len(self.staged_message)} chars)\n")
    # End-def

    ##
    # @brief `commit` command handle. 
    # Send message to LLM (transactional).
    #
    # @note The staged buffer is only cleared AFTER the LLM accepts the
    #       message (first step() succeeds). On failure the draft stays in
    #       staged.md and a recovery menu is offered:
    #       [R]etry / [S]ave and Exit / [V]im (edit) / [D]iscard.
    #
    def _cmd_commit(self):
        content = self.staged_message.strip()
        if not content:
            self.cli.error("Error: Buffer is empty. Draft a message using 'vim' or 'load' first.")
            return
        # End-if

        self.cli.raw(f"\n{self.cli.C_CYAN}================ COMMIT PREVIEW ================{self.cli.C_RESET}")
        preview = content[:500]
        self.cli.raw(preview + ("\n... [Truncated]" if len(content) > 500 else ""))
        self.cli.raw(f"{self.cli.C_CYAN}================================================{self.cli.C_RESET}")

        ans = input(f"{self.cli.C_CYAN}[?]{self.cli.C_RESET} Proceed to send to LLM? [y/N]: ").strip().lower()
        if ans not in ['y', 'yes']:
            self.cli.error("Send cancelled.")
            return
        # End-if

        # From here on a turn is running: a SIGINT is recorded as a stop request
        # (see _on_sigint) instead of raising KeyboardInterrupt.
        self._turn_active = True

        # Stale requests from a previous turn must not leak into this one, and
        # the clear must happen BEFORE the backup copy so that a SIGINT arriving
        # during the copy is preserved and honoured by the checkpoint below.
        clear_stop()

        # ----- Turn backup (the Ctrl+C rollback anchor) -----
        # @note The backup is taken BEFORE the raw input is appended, so it
        #       holds exactly "the state before this commit" - including the
        #       staged draft, which is what the user gets back after a rollback.
        # @note A retried commit (same pending turn after an API failure) reuses
        #       the existing backup instead of re-snapshotting a history that
        #       already contains the message.
        if not self._backup_ready:
            ok, msg = SessionBackup(self.session.current_session_dir).create(
                "commit", history_messages=len(self.agent.history))
            self._backup_ready = ok
            if not ok:
                self.cli.warning(f"Backup failed: this turn cannot be rolled back ({msg}).")
            # End-if
        # End-if

        # Checkpoint C0: a stop requested while the backup was being written
        # must abort BEFORE any inference or tool execution starts.
        if self._try_stop():
            return
        # End-if

        self.cli.info("Inference Engine Started...\n")

        # ----- Phase 1: send the user message (transactional) -----
        # The draft stays in staged.md until the LLM accepts the message; a
        # failed send keeps the buffer intact for Retry / Edit / Discard.
        injected = False
        while True:
            try:
                # A stop requested while the recovery menu (or the editor) was
                # open must be consumed BEFORE another step is sent: otherwise
                # the retry would still spend one LLM call and execute its
                # tools. Same rationale as checkpoint C0.
                if self._try_stop():
                    return
                # End-if

                if not injected:
                    self.agent.inject_user_message(content)
                    injected = True
                # End-if

                cont, err = self.agent.step()
                if err is None:
                    # Message accepted: the staged area is now committed.
                    self.session.clear_staged()
                    self.staged_message = ""
                    break
                # End-if

                # Stop requested during this step: never open a recovery menu
                # for a turn the user already asked to drop.
                if self._try_stop():
                    return
                # End-if

                # Recovery menu: the message was never accepted.
                choice = self._prompt_recovery_action(err)
                if choice == 'R':
                    # Message stays in history; step() re-sends it (the dynamic
                    # context block is rebuilt idempotently inside step()).
                    continue
                elif choice == 'V':
                    # Roll back the pending message, edit the draft, re-send.
                    self._rollback_pending_message()
                    if not self._cmd_vim():
                        # Editor could not be launched: keep the draft and
                        # leave the commit flow (no re-send of stale content).
                        self.cli.info("Commit aborted. Draft preserved in this branch.")
                        return
                    # End-if
                    content = self.staged_message.strip()
                    if not content:
                        self.session.clear_staged()
                        self.cli.error("Buffer is empty after edit; draft cleared. Commit aborted.")
                        return
                    # End-if
                    self.cli.raw(f"\n{self.cli.C_CYAN}=========== REVISED COMMIT PREVIEW ==========={self.cli.C_RESET}")
                    self.cli.raw(content[:500] + ("\n... [Truncated]" if len(content) > 500 else ""))
                    self.cli.raw(f"{self.cli.C_CYAN}=============================================={self.cli.C_RESET}")
                    injected = False  # re-inject the revised content
                elif choice == 'S':
                    # vim :wq semantics: the draft is saved (staged.md kept),
                    # leave the commit flow; it can be committed later.
                    self._rollback_pending_message()
                    self.cli.info("Draft saved to this branch. Commit aborted.")
                    return
                elif choice == 'D':
                    # Draft discarded.
                    self._rollback_pending_message()
                    self.session.clear_staged()
                    self.staged_message = ""
                    self.cli.info("Draft discarded.")
                    return
                # End-elif
            except (KeyboardInterrupt, EOFError):
                # Interrupt BEFORE acceptance: roll back the never-accepted
                # message and keep the draft for later.
                self._rollback_pending_message()
                self.cli.info("\nSend aborted. Draft preserved.")
                return
            # End-try
        # End-while

        # ----- Phase 2: tool loop (only when the accepted step requested
        # tools; a plain-text reply (cont=False) means the turn is done).
        # A Ctrl+C during phase 1 is consumed here: the turn is rolled back
        # instead of continuing. Without a stop request the previous contract
        # still holds - an API error leaves the pending tool turn in history so
        # run()'s background check can resume it at the next prompt. -----
        if self._try_stop():
            return
        # End-if

        if cont:
            try:
                _, err = self._run_agent_loop()
            except (KeyboardInterrupt, EOFError):
                self.cli.info("\nSend aborted mid-execution. The pending tool turn will auto-resume at the next prompt, or be dropped if it keeps failing.")
                return
            # End-try
            if err is not None:
                self.cli.error(f"Tool loop interrupted by API error: {err}")
                self.cli.info("The pending tool turn will auto-resume at the next prompt, or be dropped if it keeps failing.")
            # End-if
        # End-if

        # Last checkpoint of the turn: also covers a stop request that landed
        # on the final step (a plain-text reply with nothing left to run).
        self._try_stop()

        # @note _backup_ready is turn-scoped and must not survive a finished
        #       turn: the next commit has to take a fresh snapshot, otherwise an
        #       interrupt in a later turn would roll back this completed turn.
        #       (Paths that keep the pending message - a retried commit after an
        #       API failure or an aborted send - intentionally return early and
        #       keep reusing the existing backup.)
        self._backup_ready = False
    # End-def

    ##
     # @brief Run the agent tool-loop until it stops naturally or hits an API error.
     #
     # @return (True, None) loop ended normally.
     # @retval (False, err) an API error occurred (all bounded retries exhausted).
     #
    def _run_agent_loop(self):
        while True:
            # Checkpoint C1a: a stop requested between iterations stops the loop
            # before another LLM call is sent.
            if self._try_stop():
                return False, None
            # End-if

            cont, err = self.agent.step()

            # Checkpoint C1b: a stop requested while the step was running (or by
            # a tool through the [C] approval choice) stops the loop here.
            if self._try_stop():
                return False, None
            # End-if

            if err is not None:
                return False, err
            # End-if
            if not cont:
                return True, None
            # End-if
        # End-while
    # End-def

    ##
     # @brief Ask the user how to recover from a failed send.
     #
     # @param err API error string.
     #
     # @return Choice: 'R' (retry), 'S' (save and exit), 'V' (vim edit), 'D' (discard).
     #
    def _prompt_recovery_action(self, err):
        self.cli.error(f"\nLLM API Error: {err}")
        self.cli.info("The staged draft is preserved. Choose an action:")
        while True:
            choice = input(
                f"{self.cli.C_CYAN}[?]{self.cli.C_RESET} "
                "[R]etry / [S]ave and Exit / [V]im (edit) / [D]iscard: "
            ).strip().lower()
            if choice in ('r', 'retry'):
                return 'R'
            elif choice in ('s', 'save', 'exit'):
                return 'S'
            elif choice in ('v', 'vim', 'edit'):
                return 'V'
            elif choice in ('d', 'discard'):
                return 'D'
            # End-elif
            self.cli.error("Invalid choice. Please enter R / S / V / D.")
        # End-while
    # End-def

    ##
     # @brief Pop the injected user message that was never accepted by the LLM.
     #
     # @note On API failure step() appends nothing, so the history tail is
     #       exactly the injected plain-text user message (plus the dynamic
     #       context block, removed with it). A tool_result payload is never
     #       popped here.
     #
    def _rollback_pending_message(self):
        hist = self.agent.history
        if not hist:
            return
        # End-if
        tail = hist[-1]
        if tail.get("role") != "user":
            return
        # End-if
        if isinstance(tail.get("content", ""), list):
            return  # tool_result payload: never roll back
        # End-if
        hist.pop()
        self.session.save_history(hist)
    # End-def

    ##
     # @brief Drop the pending tool turn (trailing tool_result + its assistant
     #        tool_use) from history.
     #
     # @note The tool outputs were never sent to the LLM (the API call failed),
     #       so removing the pair loses no information and the model re-decides
     #       on the next turn. Keeps history valid: a dangling tool_use would
     #       400 the next commit (tool_use without tool_result).
     #
    def _drop_pending_tool_turn(self):
        hist = self.agent.history
        if hist and hist[-1]["role"] == "user":
            tail = hist[-1]["content"]
            if isinstance(tail, list) and tail and tail[0].get("type") == "tool_result":
                hist.pop()
                if hist and hist[-1]["role"] == "assistant":
                    hist.pop()
                # End-if
            # End-if
        # End-if
        self.session.save_history(hist)
    # End-def

    ##
     # @brief SIGINT handler: record a stop request for the running turn.
     #
     # @note At the prompt (no turn running) the default Python semantics are
     #       preserved by re-raising KeyboardInterrupt, so prompt_toolkit's
     #       Ctrl+C key binding and the EOF handling stay untouched.
     # @note While a turn runs the handler only records the request: the
     #       in-flight call finishes on its own, then a checkpoint consumes the
     #       flag (see _try_stop). Nothing is raised mid-call and no file I/O is
     #       performed here, so no half-built state can be produced.
     #
    def _on_sigint(self, signum, frame):
        if not self._turn_active:
            raise KeyboardInterrupt
        # End-if
        request_stop("sigint")
        self.cli.warning(
            f"Stop requested ({stop_reason()}): the current step will finish, "
            f"then the turn is rolled back to before this commit (a prompt is "
            f"shown if it cannot be rolled back)."
        )
    # End-def

    ##
     # @brief Consume a pending stop request at a turn checkpoint.
     #
     # @return True when the turn must stop (the rollback already ran).
     # @retval True Stop the turn now.
     # @retval False Nothing requested, or the request was refused.
     #
     # @note Safety rule: stopping is only allowed when the turn has a complete
     #       backup AND the restore succeeds. Otherwise the request is refused
     #       and the agent keeps running, because stopping a turn without
     #       restoring it could leave a tool_use without its matching
     #       tool_result (breaking the next request) or a half-restored session.
     #
    def _try_stop(self):
        if not stop_requested():
            return False
        # End-if

        if not SessionBackup(self.session.current_session_dir).is_valid():
            self.cli.error("Stop failed: no usable backup for this turn, continuing.")
            clear_stop()
            return False
        # End-if

        if not self._do_rollback():
            # The restore could not be completed; the session was put back the
            # way it was, so the turn simply keeps running.
            self.cli.error("Stop failed: the rollback could not be completed, continuing.")
            clear_stop()
            return False
        # End-if

        clear_stop()
        return True
    # End-def

    ##
     # @brief Restore the session state captured before the last commit.
     #
     # @return True when the rollback completed (state reloaded); False when
     #         nothing was rolled back and the turn should keep running.
     #
     # @note Session files (history.log / staged.md / task_state.json / memory)
     #       are restored from .log/sess_xx/backup, then the agent history and
     #       the staged draft are reloaded into memory. artifacts/ and api.log
     #       are intentionally outside the backup (see src/utils/logging/backup.py).
     # @note The restore is all-or-nothing (SessionBackup.restore): on failure
     #       the session is left exactly as it was and the in-memory history and
     #       draft are NOT reloaded, so the caller can safely continue the turn.
     # @note Workspace changes (file writes, bash, ssh) are OUT of scope: the
     #       user is told to review them with git.
     #
    def _do_rollback(self):
        restored, failed = SessionBackup(self.session.current_session_dir).restore()

        if failed:
            # A refused rollback is as important for a post-mortem as a
            # successful one, so it is audited as well.
            self.session.log_api_call("TURN ROLLBACK", {
                "trigger": stop_reason(),
                "status": "failed",
                "failed": failed,
            })
            self.cli.error("Rollback could not be completed: " + ", ".join(failed))
            self.cli.info("Nothing was rolled back; the turn keeps running.")
            return False
        # End-if

        self.agent.reload_history()
        self.staged_message = self.session.load_staged()
        self._backup_ready = False

        # Audit trail: api.log is deliberately outside the backup set, so this
        # record survives the rollback it describes and makes a post-mortem
        # possible without inferring the event from timestamps alone.
        self.session.log_api_call("TURN ROLLBACK", {
            "trigger": stop_reason(),
            "status": "done",
            "restored": restored,
            "failed": failed,
            "history_messages": len(self.agent.history),
            "draft_chars": len(self.staged_message.strip()),
        })

        self.cli.success(
            f"Rolled back to before the last commit (trigger: {stop_reason()}): "
            + ", ".join(restored))
        if failed:
            self.cli.error("Rollback reported failures: " + ", ".join(failed))
        # End-if
        self.cli.info(
            f"Original input restored to the staged buffer "
            f"({len(self.staged_message.strip())} chars). Commit to resend it, "
            f"or edit it first with 'vim'."
        )
        self.cli.warning("Workspace changes are NOT rolled back; review them yourself (git status).")
        return True
    # End-def

    ##
     # @brief Run class InteractiveCLI. 
     # Send message to LLM.
     #
    def run(self):
        self.cli.raw(f"\n{self.cli.C_CYAN}================ SHELL READY ================{self.cli.C_RESET}")

        # Ctrl+C is handled by us while a turn runs (see _on_sigint / _try_stop);
        # at the prompt the handler re-raises KeyboardInterrupt, so the input
        # layer keeps its default behaviour.
        signal.signal(signal.SIGINT, self._on_sigint)

        # Try to load HAS_PTK (tab completion)
        if HAS_PTK:
            self.cli.success("Bash-style Tab completion enabled (Powered by prompt_toolkit).")
        else:
            self.cli.error("prompt_toolkit not found. Fallback to basic input. (pip install prompt_toolkit)")
        # End-if

        # Print help
        self._print_help()

        # Track consecutive errors to prevent infinite loop of death
        consecutive_errors = 0

        # Interactive Loop
        while True:
            try:
                # ----- @par 1. Background task check (Agent running) -----

                # Checkpoint C3: a pending stop request must never be turned
                # into another continuation of the interrupted turn.
                if self._try_stop():
                    continue
                # End-if

                if self.agent.history and self.agent.history[-1]["role"] == "user":
                    content = self.agent.history[-1]["content"]
                    if isinstance(content, list) and len(content) > 0 and content[0].get("type") == "tool_result":
                        self.cli.info("\nProcessing pending tool returns in background...")

                        self._turn_active = True
                        try:
                            ok, err = self._run_agent_loop()
                        finally:
                            self._turn_active = False
                        # End-try

                        # All retries exhausted: drop the pending tool turn
                        # (tool_result + its assistant tool_use pair) so history
                        # never ends with a dangling tool_use that would 400 the
                        # next commit. The pair was never seen by the model.
                        if err is not None:
                            self.cli.error(f"\nFATAL: Background execution blocked by an API Error: {err}")
                            self._drop_pending_tool_turn()
                            self.cli.error("Dropping the pending tool turn to prevent infinite API retry loop.")
                        # End-if

                        continue
                    # End-if
                # End-if

                # 2. ----- @par UI Prompt Render -----

                meta = self.session.get_current_meta()
                branch_name = meta.get("name", "unknown")
                dirty_flag = "*" if self.staged_message.strip() else ""

                # Fetch model name and format it to be clean (e.g. "nvidia/nemotron" -> "nemotron")
                full_model_name = self.agent.config.get("MODEL_ID", "dandelion")
                short_model_name = full_model_name.split("/")[-1] if "/" in full_model_name else full_model_name

                # Linux-Style Colored Prompt Formatting
                prompt_str_ansi = (
                    f"{self.cli.C_GREEN}{short_model_name}{self.cli.C_RESET}:"
                    f"{self.cli.C_BLUE}({branch_name}"
                    f"{self.cli.C_YELLOW}{dirty_flag}"
                    f"{self.cli.C_BLUE}){self.cli.C_RESET}"
                    f"{self.cli.C_GRAY}>{self.cli.C_RESET} "
                )

                # 3. ----- @par Read user input -----

                if HAS_PTK:
                    cmd_input = self.prompt_session.prompt(
                        ANSI(prompt_str_ansi),
                        completer=self._build_completer(),
                        complete_while_typing=False
                    ).strip()
                else:
                    cmd_input = input(prompt_str_ansi).strip()
                # End-if

                # Reset error counter because we successfully reached the blocking input layer
                consecutive_errors = 0

                if not cmd_input:
                    continue

                # 4. ----- @par Parse and Dispatch-----

                # Parse command.
                try:
                    parts = shlex.split(cmd_input)
                except ValueError as e:
                    self.cli.error(f"Shell syntax error: {e}")
                    continue
                # End-try

                # Split command.
                command = parts[0].lower()
                args = parts[1:]

                # Dispatch command.
                if command in ['help', '-h']:
                    self._print_help()
                elif command in ['quit', 'exit', '-q']:
                    self.cli.info("Terminating Dandelion Shell. Goodbye.")
                    break
                elif command == 'branch':
                    self._cmd_branch(args)
                elif command == 'checkout':
                    self._cmd_checkout(args)
                elif command == 'vim':
                    self._cmd_vim()
                elif command == 'load':
                    self._cmd_load(args)
                elif command == 'status':
                    self._cmd_status()
                elif command == 'commit':
                    # The turn only starts once the user confirms the send, so
                    # _cmd_commit flips _turn_active itself. Whatever exit path
                    # it takes, neither turn-scoped flag may survive it: the
                    # next commit must start from a clean state (and take a
                    # fresh backup) instead of reusing a stale one.
                    try:
                        self._cmd_commit()
                    finally:
                        self._turn_active = False
                        self._backup_ready = False
                    # End-try
                elif command == 'clear':
                    self._cmd_clear()
                else:
                    self.cli.error(f"Unknown command '{command}'. Type 'help' for available commands.")
                # End-if
            # End-try

            # 5. ----- @par Exception Handle -----

            except KeyboardInterrupt:
                self.cli.raw("")
                continue
            except EOFError:
                self.cli.info("\nTerminating Dandelion Shell (EOF). Goodbye.")
                break
            except Exception as e:
                consecutive_errors += 1
                self.cli.error(f"\nUnexpected Error: {e}")

                # Break out if the loop is spinning wildly without user interaction
                if consecutive_errors >= 3:
                    self.cli.warning("FATAL: Too many consecutive errors. Terminating shell to prevent infinite loop.")
                    break

                self.cli.info("Shell recovered. Your staged message and session are preserved.")
                continue
        # End-while
    # End-def
# End-class