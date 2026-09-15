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
 # @note Contents: history.log, staged.md, task_state.json and memory/.
 #       api.log and meta.log are intentionally excluded (debug transcript and
 #       session-manager metadata). artifacts/ is excluded too: the stored
 #       outputs live outside the request payload, so an orphan entry left by a
 #       rolled back turn is harmless (the model may only wonder what it is),
 #       while keeping them in every turn backup would cost a full copy of a
 #       directory that can grow to megabytes. archives/ is excluded as well.
 #

import os
import json
import shutil
import datetime

##
 # @brief Session entries copied into a backup, in restore order.
 #
BACKUP_ITEMS = ["history.log", "staged.md", "task_state.json", "memory"]

##
 # @brief Directory (inside the session dir) holding the single backup.
 #
BACKUP_DIRNAME = "backup"

##
 # @brief Completeness marker written LAST by create().
 #
BACKUP_MARKER = "backup.json"

##
 # @brief Prefix of the per-attempt parking directory used by restore().
 #
SWAP_PREFIX = ".swap."

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
     # @note Swap-based restore, in three phases:
     #       1. every live item is MOVED aside into a per-attempt parking
     #          directory (backup/.swap.<timestamp>),
     #       2. the backup items are copied into the session directory,
     #       3. the parked originals are dropped.
     #       A failure in phase 2 therefore never destroys the previous state:
     #       the parked copy is kept and its path is reported, instead of the
     #       live item being deleted before its replacement is known to work.
     #
     # @note Items missing from the backup are not recreated, so the result is
     #       "the state as it was then" rather than an overlay of old and new.
     #
     # @note Per-item failures are collected and reported to the caller; a
     #       single bad entry never aborts the whole restore.
     #
    def restore(self):
        if not self.is_valid():
            return [], ["no usable backup"]
        # End-if

        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        swap_dir = os.path.join(self.backup_dir, f"{SWAP_PREFIX}{stamp}")

        # ----- Phase 1: park the current live items -----
        parked = []
        for item in BACKUP_ITEMS:
            live = os.path.join(self.session_dir, item)
            if not os.path.exists(live):
                continue
            # End-if
            try:
                os.makedirs(swap_dir, exist_ok=True)
                shutil.move(live, os.path.join(swap_dir, item))
                parked.append(item)
            except Exception as e:
                return [], [f"{item}: cannot park live copy: {e}"]
            # End-try
        # End-for

        # ----- Phase 2: copy the backup into the session directory -----
        restored = []
        failed = []
        for item in BACKUP_ITEMS:
            saved = os.path.join(self.backup_dir, item)
            if not os.path.exists(saved):
                continue
            # End-if
            live = os.path.join(self.session_dir, item)
            try:
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

        # ----- Phase 3: drop the parked originals (kept when phase 2 failed) --
        if failed:
            failed.append(f"previous state kept at {swap_dir}")
        else:
            for item in parked:
                target = os.path.join(swap_dir, item)
                try:
                    if os.path.isdir(target):
                        shutil.rmtree(target)
                    elif os.path.isfile(target):
                        os.remove(target)
                    # End-if
                except Exception:
                    pass  # leftover parking data is harmless
                # End-try
            # End-for
            if os.path.isdir(swap_dir) and not os.listdir(swap_dir):
                os.rmdir(swap_dir)
            # End-if
        # End-if
        return restored, failed
    # End-def
# End-class
