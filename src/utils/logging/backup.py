##
 # @file src/utils/logging/backup.py
 # @date 2026/09/15
 # 
 # @brief Turn-scoped session backup used by the Ctrl+C rollback.
 #
 # @note Plain file copies only (no symlinks, no hardlinks), so the behaviour is
 #       identical on Linux / macOS / Windows.
 #
 # @note The backup is the rollback BOUNDARY: it captures the session state as
 #       it was BEFORE the last raw user input was appended. Restoring it is
 #       therefore correct no matter where the interrupt landed, and no message
 #       bookkeeping (indices, pairing, compaction handling) is needed.
 #
 # @note api.log and meta.log are intentionally excluded: the first is a debug
 #       transcript of the LLM traffic, the second belongs to the session
 #       manager. archives/ is excluded too: a rollback can leave one orphan
 #       archive behind, which nothing reads and which helps post-mortems.
 #

import os
import json
import shutil
import datetime

##
 # @brief Session entries copied into a backup, in restore order.
 #
BACKUP_ITEMS = ["history.log", "staged.md", "task_state.json", "memory", "artifacts"]

##
 # @brief Directory (inside the session dir) holding the single backup.
 #
BACKUP_DIRNAME = "backup"

##
 # @brief Completeness marker written LAST by create().
 #
BACKUP_MARKER = "backup.json"

##
 # @brief Session backup: create / validate / restore.
 #
class SessionBackup:
    ##
     # @brief Constructor.
     #
     # @param session_dir Current session directory (.log/sess_xx), may be None.
     #
    def __init__(self, session_dir):
        self.session_dir = session_dir
        self.backup_dir = os.path.join(session_dir, BACKUP_DIRNAME) if session_dir else None
    # End-def

    ##
     # @brief Check that a complete backup is available.
     #
     # @return True when the marker is readable and marked complete.
     #
     # @note The marker is written after every item was copied, so a backup
     #       interrupted half-way reports as invalid and is never restored.
     #
    def is_valid(self):
        if not self.backup_dir:
            return False
        # End-if
        marker = os.path.join(self.backup_dir, BACKUP_MARKER)
        if not os.path.isfile(marker):
            return False
        # End-if
        try:
            with open(marker, "r", encoding="utf-8") as f:
                meta = json.load(f)
            return isinstance(meta, dict) and meta.get("status") == "complete"
        except Exception:
            return False
        # End-try
    # End-def

    ##
     # @brief Rebuild the backup from the live session state.
     #
     # @param note Free-form tag stored in the marker (default "commit").
     # @param history_messages Message count of the live history (report only).
     #
     # @return (ok, message) tuple; ok is False on any failure.
     #
     # @note The directory is rebuilt from scratch every turn: a single backup
     #       is enough for "undo the last commit", and it keeps the on-disk
     #       footprint flat (one copy, no history of copies).
     #
    def create(self, note="commit", history_messages=None):
        if not self.session_dir or not os.path.isdir(self.session_dir):
            return False, "no session directory"
        # End-if
        try:
            if os.path.isdir(self.backup_dir):
                shutil.rmtree(self.backup_dir)
            # End-if
            os.makedirs(self.backup_dir)

            items = []
            for item in BACKUP_ITEMS:
                src = os.path.join(self.session_dir, item)
                dst = os.path.join(self.backup_dir, item)
                if os.path.isdir(src):
                    shutil.copytree(src, dst)
                    items.append(item)
                elif os.path.isfile(src):
                    shutil.copy2(src, dst)
                    items.append(item)
                # End-if
            # End-for

            # Marker LAST: it is the only completeness witness (see is_valid).
            meta = {
                "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "note": note,
                "items": items,
                "history_messages": history_messages,
                "status": "complete",
            }
            with open(os.path.join(self.backup_dir, BACKUP_MARKER), "w",
                      encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            # End-with
            return True, f"{len(items)} item(s) saved"
        except Exception as e:
            return False, str(e)
        # End-try
    # End-def

    ##
     # @brief Restore the session state from the backup.
     #
     # @return (restored, failed) lists of item names.
     #
     # @note Each item is removed from the live session first and copied back
     #       afterwards, so the result is the state "as it was then" instead of
     #       an overlay of new and old files. Items missing from the backup are
     #       left deleted (equivalent to "that file did not exist yet").
     #
     # @note Per-item failures are collected and reported to the caller; a
     #       single bad entry never aborts the whole restore.
     #
    def restore(self):
        if not self.is_valid():
            return [], ["no usable backup"]
        # End-if

        restored = []
        failed = []
        for item in BACKUP_ITEMS:
            live = os.path.join(self.session_dir, item)
            saved = os.path.join(self.backup_dir, item)
            try:
                if os.path.isdir(live):
                    shutil.rmtree(live)
                elif os.path.isfile(live):
                    os.remove(live)
                # End-if

                if os.path.isdir(saved):
                    shutil.copytree(saved, live)
                elif os.path.isfile(saved):
                    shutil.copy2(saved, live)
                # End-if
                restored.append(item)
            except Exception as e:
                failed.append(f"{item}: {e}")
            # End-try
        # End-for
        return restored, failed
    # End-def
# End-class
