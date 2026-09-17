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
 # @note Restore is all-or-nothing: on any failure the session is returned to
 #       exactly the state it had before the attempt (see _unpark), so a failed
 #       rollback never leaves a half-restored session behind.
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
 # @brief Entries that are directories inside the backup (all others are files).
 #
BACKUP_DIR_ITEMS = {"memory"}

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
     # @brief Check that a complete and usable backup is available.
     #
     # @return True when the marker is complete AND every recorded item exists
     #         in the backup with the expected type.
     #
     # @note The marker is written after every item was copied, so a backup
     #       interrupted half-way reports as invalid and is never restored.
     # @note The item check matters: restore() treats a missing backup entry as
     #       "did not exist yet", parks the live entry and then drops the parked
     #       copy. Without this check a damaged backup would therefore silently
     #       delete the live history/staged/task_state/memory entry.
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
        except Exception:
            return False
        # End-try

        if not isinstance(meta, dict) or meta.get("status") != "complete":
            return False
        # End-if

        items = meta.get("items")
        if not isinstance(items, list):
            return False
        # End-if

        for item in items:
            if not isinstance(item, str) or item not in BACKUP_ITEMS:
                return False
            # End-if
            path = os.path.join(self.backup_dir, item)
            if item in BACKUP_DIR_ITEMS:
                if not os.path.isdir(path):
                    return False
                # End-if
            elif not os.path.isfile(path):
                return False
            # End-if
        # End-for
        return True
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
     # @return (restored, failed) lists of item names / failure messages.
     #
     # @note Swap-based, all-or-nothing restore, in three phases:
     #       1. every live item is MOVED aside into a per-attempt parking
     #          directory (backup/.swap.<timestamp>),
     #       2. the backup items are copied into the session directory,
     #       3. the parked originals are dropped.
     #       If phase 1 or phase 2 fails, the session is put back the way it was
     #       (the fresh copies are removed and every parked original is moved
     #       back), so the caller can keep running the turn instead of leaving a
     #       half-restored session behind. Nothing unique is ever lost: a copy
     #       made in phase 2 is removed, and every original stays in the parking
     #       directory until phase 3.
     #
     # @note Items missing from the backup are not recreated, so the result is
     #       "the state as it was then" rather than an overlay of old and new.
     #       is_valid() guarantees that every RECORDED item is present, so this
     #       only applies to entries that legitimately did not exist yet.
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
                failures = [f"{item}: cannot park live copy: {e}"]
                failures.extend(self._unpark(parked, swap_dir))
                return [], failures
            # End-try
        # End-for

        # ----- Phase 2: copy the backup into the session directory -----
        copied = []
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
                copied.append(item)
            except Exception as e:
                failures = [f"{item}: {e}"]
                # A failed copy can leave a partial target behind, and the item
                # is not registered in `copied` yet (that happens only after a
                # successful copy), so it must be removed explicitly here - it
                # is neither covered by _undo_copies() nor by _unpark() when the
                # entry did not exist in the live session before phase 1.
                failures.extend(self._remove_live(item))
                failures.extend(self._undo_copies(copied))
                failures.extend(self._unpark(parked, swap_dir))
                return [], failures
            # End-try
        # End-for

        # ----- Phase 3: drop the parked originals -----
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
            try:
                os.rmdir(swap_dir)
            except Exception:
                pass
            # End-try
        # End-if
        return copied, []
    # End-def

    ##
     # @brief Delete the live copy of one item (no-op when it does not exist).
     #
     # @param item Item name relative to the session directory.
     #
     # @return List of failure messages (empty when the path is gone).
     #
     # @note Used by the failure recovery of restore(): a partially created
     #       target is indistinguishable from a complete one here, so it is
     #       simply removed - the authoritative copy is either parked (phase 1)
     #       or still inside the backup, never lost by this call.
     #
    def _remove_live(self, item):
        failures = []
        live = os.path.join(self.session_dir, item)
        try:
            if os.path.isdir(live):
                shutil.rmtree(live)
            elif os.path.isfile(live):
                os.remove(live)
            # End-if
        except Exception as e:
            failures.append(f"{item}: cannot remove the live copy: {e}")
        # End-try
        return failures
    # End-def

    ##
     # @brief Remove the entries created by phase 2 (their originals are parked).
     #
     # @param copied Item names copied into the session directory by phase 2.
     #
     # @return List of failure messages (empty when the copies were removed).
     #
    def _undo_copies(self, copied):
        failures = []
        for item in copied:
            failures.extend(self._remove_live(item))
        # End-for
        return failures
    # End-def

    ##
     # @brief Move the parked originals back and drop the parking directory.
     #
     # @param parked Item names parked by phase 1.
     # @param swap_dir Parking directory of this attempt.
     #
     # @return List of failure messages (empty when the previous state was put
     #         back completely).
     #
    def _unpark(self, parked, swap_dir):
        failures = []
        for item in parked:
            saved = os.path.join(swap_dir, item)
            live = os.path.join(self.session_dir, item)
            try:
                failures.extend(self._remove_live(item))
                shutil.move(saved, live)
            except Exception as e:
                failures.append(f"{item}: cannot move the parked original back: {e}")
            # End-try
        # End-for
        if os.path.isdir(swap_dir):
            if os.listdir(swap_dir):
                failures.append(f"previous state kept at {swap_dir}")
            else:
                try:
                    os.rmdir(swap_dir)
                except Exception:
                    pass
                # End-try
            # End-if
        # End-if
        return failures
    # End-def
# End-class
