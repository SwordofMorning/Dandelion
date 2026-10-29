##
 # @file src/remote/execution/loop.py
 # @date 2026/10/27
 #
 # @brief Single blocking interrupt loop and daily session rollover for Dandelion Remote.
 # Blocks on a condition variable until woken by user messages, CLI session syncs,
 # completed background jobs, or due schedule timers, and performs daily session
 # rotation with SubAgent-pruned context inheritance.
 #

import copy
import datetime as dt
import json
import os
import shutil
import threading

from src.core.agent.prune import ContextPruner
from src.core.utils.logging.backup import SessionBackup


##
 # @brief Single blocking interrupt loop serializing main-agent execution.
 #
class RemoteLoop:
    ##
     # @brief Bind lifecycle storage, worker execution, agent construction and notifications.
     #
     # @param store Shared durable store with its condition variable.
     # @param jobs Background job manager used by remote delegation tools.
     # @param agent_factory Callable creating a core agent for one conversation.
     # @param notify Callback persisting or emitting a conversation reply.
     # @param backups Optional shared BackupStore for tracking sync digest completion.
     #
     # @return None.
     #
    def __init__(self, store, jobs, agent_factory, notify, backups=None):
        self.store = store
        self.jobs = jobs
        self.agent_factory = agent_factory
        self.notify = notify
        self.backups = backups
        self._stop = threading.Event()
        self._agents = {}
        self._session_dates = {}
        self._agents_lock = threading.RLock()
    # End-def

    ##
     # @brief Persist a user, analysis or sync_received interrupt event.
     #
     # @param kind Event category ("message", "analysis", or "sync_received").
     # @param session_id Conversation identifier used to scope persisted records.
     # @param payload Event text or dictionary payload.
     # @param event_id Optional deterministic event identifier for deduplication.
     #
     # @return Persisted event identifier.
     #
    def post(self, kind, session_id, payload, event_id=None):
        if kind not in ("message", "analysis", "sync_received"):
            raise ValueError("External events must be message, analysis, or sync_received")
        # End-if
        if isinstance(payload, str):
            payload = {"text": payload}
        # End-if
        if not isinstance(payload, dict) or (
                kind != "sync_received" and not str(payload.get("text", "")).strip()):
            raise ValueError("Event text is required")
        # End-if
        return self.store.add_event(kind, str(session_id), payload, event_id)
    # End-def

    ##
     # @brief Request cancellation of queued and active work for a conversation.
     #
     # @param session_id Conversation identifier whose turn should stop.
     #
     # @return True when pending events or an active agent turn were signaled.
     #
    def cancel(self, session_id):
        cancelled_pending = self.store.cancel_pending_events(str(session_id)) > 0
        with self._agents_lock:
            agent = self._agents.get(str(session_id))
            cancel_event = getattr(agent, "_remote_cancel_event", None)
            if cancel_event is not None:
                cancel_event.set()
                return True
            # End-if
            return cancelled_pending
        # End-with
    # End-def

    ##
     # @brief Construct or reuse a conversation agent on its owner thread.
     #
     # @param session_id Conversation identifier used to scope persisted records.
     #
     # @return Conversation agent bound to the supplied session_id.
     #
    def _agent(self, session_id):
        with self._agents_lock:
            agent = self._agents.get(session_id)
            if agent is None:
                agent = self.agent_factory(session_id)
                self._agents[session_id] = agent
                meta = agent.session.get_current_meta() if agent.session.current_session_dir else {}
                created_day = str(meta.get("created_at", ""))[:10]
                self._session_dates[session_id] = created_day or dt.date.today().isoformat()
            # End-if
            return agent
        # End-with
    # End-def

    ##
     # @brief Perform daily session rollover with SubAgent-pruned history inheritance.
     #
     # @param agent Active conversation MyAgent instance.
     # @param session_id Conversation identifier.
     #
     # @return None.
     #
    def _check_daily_rollover(self, agent, session_id):
        today = dt.date.today().isoformat()
        last_day = self._session_dates.get(session_id, today)
        if last_day == today:
            return
        # End-if
        if not agent.history:
            self._session_dates[session_id] = today
            return
        # End-if

        old_dir = agent.session.current_session_dir
        old_history = copy.deepcopy(agent.history)
        old_state_file = os.path.join(old_dir, "task_state.json") if old_dir else None

        # Prepare pruned history BEFORE creating today's session so an interrupted
        # model call never leaves an empty new session behind.
        pruner = ContextPruner(
            safe_client=agent.client,
            session_manager=agent.session,
            runtime=agent.runtime,
            config=agent.config,
        )
        pruned_history, _ = pruner.prune(
            old_history,
            focus_hint=(
                f"Daily session rollover ({last_day} -> {today}): fold verbose tool outputs "
                "and drop completed transient turns while preserving active context and tasks."
            ),
            keep_recent_units=6,
            in_flight_tail=False,
            tag="daily_rollover",
        )

        # Create today's new session branch and immediately populate history + task_state.
        agent.session.create_session(name=f"dr_{today}")
        new_dir = agent.session.current_session_dir
        if old_state_file and os.path.exists(old_state_file) and new_dir:
            try:
                shutil.copy2(old_state_file, os.path.join(new_dir, "task_state.json"))
            except OSError:
                pass
            # End-try
        # End-if
        agent.session.save_history(pruned_history)
        agent.reload_history()
        self._session_dates[session_id] = today
    # End-def

    ##
     # @brief Format an interrupt event envelope with bounded payload and UTC clock.
     #
     # @param event Claimed durable event dictionary.
     #
     # @return Formatted interrupt user message string.
     #
    @staticmethod
    def _message(event):
        utc_now = dt.datetime.now(dt.timezone.utc)
        cn_tz = dt.timezone(dt.timedelta(hours=8))
        now = f"{utc_now.astimezone(cn_tz).strftime('%Y-%m-%d %H:%M:%S+08:00')} ({utc_now.isoformat()})"
        kind = event["kind"]
        payload = event["payload"]

        if kind == "job_done":
            header = (
                f"[Interrupt: Background Job Finished | job_id={payload.get('job_id', '')} | "
                f"status={payload.get('status', '')} | time={now}]\n"
                "(Reference data from background SubAgent; use 'send_message' if you want to report this to the user.)\n"
            )
            body = json.dumps(payload, ensure_ascii=False)[:16000]
            return header + body
        # End-if

        if kind == "sync_received":
            header = (
                f"[Interrupt: CLI Session Synced | session_id={payload.get('session_id', '')} | "
                f"device={payload.get('device', '')} | project={payload.get('project', '')} | "
                f"path={payload.get('path', '')} | time={now}]\n"
                "A local Dandelion CLI session was synced. Inspect the files in its directory "
                "(meta.log, task_state.json, history.log), write/update summary.md inside that directory, "
                "and update your memory ('remember'/'forget'). Call 'send_message' only if you want to "
                "proactively share an insight or ask the user a question."
            )
            return header
        # End-if

        if kind == "analysis":
            header = (
                f"[Interrupt: Scheduled Wakeup | schedule_id={payload.get('schedule_id', '')} | time={now}]\n"
                "(Scheduled wakeup. Call 'send_message' if you decide to contact the user, or work silently.)\n"
            )
            return header + str(payload.get("text", ""))
        # End-if

        return (
            f"[Interrupt: User Message | session_id={event['session_id']} | time={now}]\n"
            "(Plain-text output stays in history.log only; call 'send_message' to reply to the user.)\n"
            + str(payload.get("text", ""))
        )
    # End-def

    ##
     # @brief Execute one interrupt turn with rollback on user cancel and tool-based messaging.
     #
     # @param event Claimed durable event dictionary.
     #
     # @return (completed_turn, error_notice_or_none) tuple.
     #
    def _handle(self, event):
        from src.core.agent.runner import run_agent_loop
        from .agent import repair_interrupted_tools

        if event["kind"] == "job_done" and event["payload"].get("status") in ("cancelled", "interrupted"):
            payload = event["payload"]
            msg = (
                f"Background task {payload.get('job_id', '')} {payload['status']}. "
                "Already completed external actions were not rolled back."
            )
            return True, msg
        # End-if

        session_id = event["session_id"]
        agent = self._agent(session_id)
        cancel = getattr(agent, "_remote_cancel_event", None)
        if cancel is not None:
            cancel.clear()
        # End-if

        self._check_daily_rollover(agent, session_id)
        repair_interrupted_tools(agent)

        if agent.runtime.cancelled() and not self._stop.is_set():
            return True, "Turn cancelled before execution."
        # End-if

        backup = SessionBackup(agent.session.current_session_dir)
        backup_ok, _ = backup.create("remote_turn", history_messages=len(agent.history))

        typing_stop = threading.Event()
        if event["kind"] == "message" and str(session_id).startswith("telegram:"):
            tg = getattr(getattr(self.notify, "__self__", None), "telegram", None)
            if tg is not None and hasattr(tg, "send_typing"):
                chat_id = str(session_id).split(":", 1)[1]

                ##
                 # @brief Refresh Telegram typing action every 4s until the turn finishes.
                 #
                 # @return None.
                 #
                def keep_typing():
                    while not typing_stop.is_set() and not self._stop.is_set():
                        tg.send_typing(chat_id)
                        typing_stop.wait(4.0)
                    # End-while
                # End-def

                threading.Thread(target=keep_typing, name="telegram-typing", daemon=True).start()
            # End-if
        # End-if

        agent.inject_user_message(self._message(event))
        try:
            ok, error = run_agent_loop(
                agent,
                should_stop=lambda: self._stop.is_set() or agent.runtime.cancelled(),
            )
        finally:
            typing_stop.set()
        # End-try

        # Process shutdown (SIGTERM/SIGINT): leave event in 'processing' for recover().
        if self._stop.is_set():
            return False, None
        # End-if

        sync_sess = event["payload"].get("session_id", "") if event["kind"] == "sync_received" else ""
        sync_snap = event["payload"].get("snapshot_id", "") if event["kind"] == "sync_received" else ""

        if error is not None:
            if sync_sess and sync_snap and self.backups is not None:
                self.backups.finalize_digest(sync_sess, sync_snap, success=False)
            # End-if
            return True, "Agent failed: " + str(error)
        # End-if

        if not ok:
            if backup_ok:
                backup.restore()
                agent.reload_history()
            # End-if
            if sync_sess and sync_snap and self.backups is not None:
                self.backups.finalize_digest(sync_sess, sync_snap, success=False)
            # End-if
            return True, "Turn cancelled and rolled back."
        # End-if

        if sync_sess and sync_snap and self.backups is not None:
            self.backups.finalize_digest(sync_sess, sync_snap, success=True)
        # End-if

        return True, None
    # End-def

    ##
     # @brief Run the single blocking interrupt loop until stopped.
     #
     # @return None.
     #
    def run(self):
        self.jobs.start(notify=self.notify)
        while not self._stop.is_set():
            self.store.fire_due_schedules(notify=self.notify)
            with self.store.changed:
                event = self.store.claim_event()
                if event is None:
                    delay = self.store.next_schedule_delay(max_wait=1.0)
                    self.store.changed.wait(timeout=delay)
                    continue
                # End-if
            # End-with

            try:
                completed, fallback_reply = self._handle(event)
            except Exception as exc:
                completed = True
                fallback_reply = "Remote event failed: " + str(exc)
            # End-try

            if not completed and self._stop.is_set():
                break
            # End-if

            if fallback_reply:
                try:
                    self.notify(event["session_id"], fallback_reply)
                except Exception:
                    pass
                # End-try
            # End-if
            self.store.complete_event(event["id"])
        # End-while
    # End-def

    ##
     # @brief Wake the blocking loop and cooperatively stop active execution.
     #
     # @return None.
     #
    def stop(self):
        self._stop.set()
        with self._agents_lock:
            for agent in self._agents.values():
                cancel = getattr(agent, "_remote_cancel_event", None)
                if cancel is not None:
                    cancel.set()
                # End-if
            # End-for
        # End-with
        with self.store.changed:
            self.store.changed.notify_all()
        # End-with
    # End-def
# End-class
