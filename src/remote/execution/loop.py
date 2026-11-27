##
 # @file src/remote/execution/loop.py
 # @date 2026/10/27
 #
 # @brief Single blocking interrupt loop for Dandelion Remote.
 # Blocks on a condition variable until woken by user messages, CLI session syncs,
 # completed background jobs, or due schedule timers, leaving session pruning and
 # rollover timing to the LLM via explicit tool calls ("Let LLM Decide").
 #

import datetime as dt
import json
import threading

from dandelion_core.host import BaseAgentHost
from dandelion_core.utils.logging.backup import SessionBackup


##
 # @brief Single blocking interrupt loop serializing main-agent execution.
 #
class RemoteLoop(BaseAgentHost):
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
        super().__init__()
        self.store = store
        self.jobs = jobs
        self.agent_factory = agent_factory
        self.notify = notify
        self.backups = backups
        self._stop = threading.Event()
        self._agents = {}
        self._active_session = None
        self._active_kind = None
        self._active_event_id = None
        self._agents_lock = threading.RLock()
    # End-def

    ##
     # @brief Resolve the TurnInterrupt controller owned by a conversation agent's runtime.
     #
     # @param agent Conversation agent instance or None.
     #
     # @return TurnInterrupt controller or None.
     #
    @staticmethod
    def _get_interrupt(agent):
        if agent is None:
            return None
        # End-if
        runtime = getattr(agent, "runtime", None)
        return getattr(runtime, "interrupt", None)
    # End-def

    ##
     # @brief Persist a user, analysis or sync_received interrupt event and preempt active turns atomically.
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
        target = str(session_id)

        ##
         # @brief Signal Stop preemption under the store lock only when a distinct new event was inserted.
         #
         # @param inserted_id Newly inserted event identifier.
         #
         # @return None.
         #
        def on_inserted(inserted_id):
            if kind not in ("message", "sync_received"):
                return
            # End-if
            with self._agents_lock:
                if self._active_session != target or self._active_event_id == inserted_id:
                    return
                # End-if
                if (kind == "message" and self._active_kind in ("message", "sync_received")) or (
                        kind == "sync_received" and self._active_kind == "sync_received"):
                    agent = self._agents.get(target)
                    cancel = self._get_interrupt(agent)
                    if cancel is not None and hasattr(cancel, "request"):
                        cancel.request("user_preempt", mode="stop")
                    # End-if
                # End-if
            # End-with
        # End-def

        return self.store.add_event(kind, target, payload, event_id, on_inserted=on_inserted)
    # End-def

    ##
     # @brief Request rollback cancellation of queued and active work for a conversation.
     #
     # @param session_id Conversation identifier whose turn should stop and roll back.
     #
     # @return True when pending events or an active agent turn were signaled.
     #
    def cancel(self, session_id):
        target = str(session_id)
        with self.store.changed, self._agents_lock:
            cancelled_pending = self.store.cancel_pending_events(target) > 0
            if self._active_session == target:
                agent = self._agents.get(target)
                cancel_event = self._get_interrupt(agent)
                if cancel_event is not None:
                    if hasattr(cancel_event, "request"):
                        cancel_event.request("user_cancel", mode="rollback")
                    else:
                        cancel_event.set()
                    # End-if
                    return True
                # End-if
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
            # End-if
            return agent
        # End-with
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
            sessions = payload.get("sessions") if isinstance(payload, dict) else None
            if isinstance(sessions, list) and len(sessions) > 1:
                items_str = "\n".join(
                    f"- session_id={s.get('session_id', '')} | device={s.get('device', '')} | "
                    f"project={s.get('project', '')} | path={s.get('path', '')}"
                    for s in sessions if isinstance(s, dict)
                )
                return (
                    f"[Interrupt: CLI Sessions Synced (Batch of {len(sessions)}) | time={now}]\n"
                    f"{items_str}\n"
                    "Multiple local Dandelion CLI sessions were synced. Inspect the files in each directory "
                    "(meta.log, task_state.json, history.log), write/update summary.md inside each directory, "
                    "and update your memory ('remember'/'forget'). Call 'send_message' only if you want to "
                    "proactively share an insight or ask the user a question."
                )
            # End-if
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
        from dandelion_core.agent.runner import run_agent_loop

        if event["kind"] == "job_done" and event["payload"].get("status") in ("cancelled", "interrupted"):
            payload = event["payload"]
            msg = (
                f"Background task {payload.get('job_id', '')} {payload['status']}. "
                "Already completed external actions were not rolled back."
            )
            return True, msg
        # End-if

        session_id = str(event["session_id"])
        with self._agents_lock:
            agent = self._agent(session_id)
            cancel = self._get_interrupt(agent)
            if self._active_event_id != event.get("id") and cancel is not None:
                cancel.clear()
            # End-if
            self._active_session = session_id
            self._active_kind = event["kind"]
            self._active_event_id = event.get("id")
        # End-with

        tg = getattr(getattr(self.notify, "__self__", None), "telegram", None)
        chat_id = session_id.split(":", 1)[1] if session_id.startswith("telegram:") else ""
        try:
            self.repair_interrupted_tools(agent)

            captured = self.capture_turn_backup(
                session=agent.session,
                note="remote_turn",
                history_messages=len(agent.history),
            )
            if isinstance(captured, tuple):
                backup, backup_ok, _ = captured
            else:
                backup = captured
                backup_ok = backup.is_valid()
            # End-if

            if event["kind"] == "message" and chat_id and tg is not None and hasattr(tg, "start_progress"):
                tg.start_progress(chat_id)
            # End-if

            m_query = str(event["payload"].get("text", "")) if event["kind"] == "message" else None
            agent.inject_user_message(self._message(event), memory_query=m_query)
            ok, error = run_agent_loop(
                agent,
                should_stop=lambda: self._stop.is_set() or agent.runtime.cancelled(),
            )
        finally:
            if chat_id and tg is not None and hasattr(tg, "end_progress"):
                tg.end_progress(chat_id)
            # End-if
            with self._agents_lock:
                self._active_session = None
                self._active_kind = None
                self._active_event_id = None
            # End-with
        # End-try

        # Process shutdown (SIGTERM/SIGINT): leave event in 'processing' for recover().
        if self._stop.is_set():
            return False, None
        # End-if

        sync_entries = []
        if event["kind"] == "sync_received" and isinstance(event.get("payload"), dict):
            raw_list = event["payload"].get("sessions") or [event["payload"]]
            sync_entries = [
                (str(s.get("session_id", "")), str(s.get("snapshot_id", "")))
                for s in raw_list
                if isinstance(s, dict) and s.get("session_id") and s.get("snapshot_id")
            ]
        # End-if

        if error is not None:
            if self.backups is not None:
                for s_id, snap_id in sync_entries:
                    self.backups.finalize_digest(s_id, snap_id, success=False)
                # End-for
            # End-if
            return True, "Agent failed: " + str(error)
        # End-if

        if not ok:
            mode = cancel.mode() if cancel is not None and hasattr(cancel, "mode") else "rollback"
            if mode == "stop":
                self.stop_active_turn("[Turn paused at step boundary: preempted by newer event.]", agent=agent)
                with self.store.changed, self._agents_lock:
                    cur_mode = cancel.mode() if cancel is not None and hasattr(cancel, "mode") else "rollback"
                    if cur_mode == "stop":
                        if event["kind"] == "sync_received" and event.get("id"):
                            self.store.requeue_event(event["id"])
                            return False, None
                        # End-if
                        return True, None
                    # End-if
                # End-with
            # End-if
            if backup_ok:
                self.rollback_active_turn(backup, "remote_cancel", agent=agent)
            # End-if
            if self.backups is not None:
                for s_id, snap_id in sync_entries:
                    self.backups.finalize_digest(s_id, snap_id, success=False)
                # End-for
            # End-if
            return True, "Turn cancelled and rolled back."
        # End-if

        if self.backups is not None:
            for s_id, snap_id in sync_entries:
                self.backups.finalize_digest(s_id, snap_id, success=True)
            # End-for
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
                with self._agents_lock:
                    sess = str(event["session_id"])
                    existing = self._agents.get(sess)
                    cancel = self._get_interrupt(existing)
                    if cancel is not None:
                        cancel.clear()
                    # End-if
                    self._active_session = sess
                    self._active_kind = event["kind"]
                    self._active_event_id = event["id"]
                # End-with
            # End-with

            try:
                completed, fallback_reply = self._handle(event)
            except Exception as exc:
                completed = True
                fallback_reply = "Remote event failed: " + str(exc)
            # End-try

            if not completed:
                if self._stop.is_set():
                    break
                # End-if
                continue
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
                cancel = self._get_interrupt(agent)
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
