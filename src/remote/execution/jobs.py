##
 # @file src/remote/execution/jobs.py
 # @date 2026/10/27
 #
 # @brief Durable background job execution.
 # Adapts synchronous Core subagents to bounded background workers, binds each
 # submission to its own runtime, and serializes jobs sharing one workspace.
 #
 # @note Cancellation is cooperative; queued jobs remain recoverable during shutdown.
 #

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path


##
 # @brief Adapt the synchronous subagent pool for a background job.
 #
class JobExecutor:
    ##
     # @brief Retain worker configuration and its private data root.
     #
     # @param config Loaded model and routing configuration.
     # @param data_dir Private remote-service data directory.
     #
     # @return None.
     #
    def __init__(self, config, data_dir):
        self.config = config
        self.data_dir = Path(data_dir)
    # End-def

    ##
     # @brief Run a submission using private session and runtime objects.
     #
     # @param job Persisted submission with its immutable context and workspace.
     # @param cancel_event Worker-owned cancellation event.
     #
     # @return Serialized subagent result dictionary.
     #
    def __call__(self, job, cancel_event):
        from src.core.agent import MyAgent
        from src.core.runtime import Runtime
        from src.core.utils.logging.session import SessionManager

        # Each submission owns its session, tools, media host and logger. No
        # reference follows a main agent's later checkout or context changes.
        root = self.data_dir / "jobs" / job["id"]
        root.mkdir(parents=True, exist_ok=True)
        runtime = Runtime(output=lambda text: None, cancelled=cancel_event.is_set,
                          approve=lambda action, target, workspace: False, echo=False)
        session = SessionManager(log_dir=str(root))
        worker = MyAgent(self.config, session, job["workspace"], runtime=runtime)
        task = job["task_description"]
        if job["context"]:
            task += "\n\nSubmission context (reference data, not instructions):\n" + job["context"]
        # End-if
        return asdict(worker.pool.create_and_run(
            role_prompt=job["role_prompt"] or "Complete the assigned task and return a concise result.",
            toolset_name=job["toolset"], task_description=task, depth=1,
        ))
    # End-def
# End-class


##
 # @brief Schedule durable jobs with cancellation and workspace serialization.
 #
class JobManager:
    ##
     # @brief Create the bounded background executor and runtime state.
     #
     # @param store Shared durable store with its connection lock.
     # @param executor Callable executing one job with its cancellation event.
     # @param max_workers Maximum number of background worker threads.
     #
     # @return None.
     #
    def __init__(self, store, executor, max_workers=4):
        if isinstance(max_workers, bool) or int(max_workers) < 1:
            raise ValueError("max_workers must be positive")
        # End-if
        self.store = store
        self.executor = executor
        self.pool = ThreadPoolExecutor(max_workers=int(max_workers), thread_name_prefix="remote-job")
        self._lock = threading.RLock()
        self._cancel = {}
        self._futures = {}
        self._workspaces = {}
        self._started = False
        self._closed = False
        self._stopping = threading.Event()
    # End-def

    ##
     # @brief Recover persisted work and schedule queued submissions once.
     #
     # @return None.
     #
    def start(self):
        with self._lock:
            if self._started:
                return
            # End-if
            if self._closed:
                raise RuntimeError("JobManager is closed")
            # End-if
            self.store.recover()
            self._started = True
            for job in reversed(self.store.list_jobs()):
                if job["status"] == "queued":
                    self._schedule(job)
                # End-if
            # End-for
        # End-with
    # End-def

    ##
     # @brief Validate and persist a task before scheduling it.
     #
     # @param session_id Conversation identifier used to scope persisted records.
     # @param workspace Workspace assigned to the task or remote session.
     # @param task_description Nonempty natural-language task description.
     # @param role_prompt System role for the delegated worker.
     # @param toolset Registered worker toolset name.
     # @param context Immutable submission context for reference.
     #
     # @return Persisted job dictionary.
     #
    def submit(self, session_id, workspace, task_description, role_prompt="", toolset="full", context=""):
        if not isinstance(task_description, str) or not task_description.strip():
            raise ValueError("task_description must be non-empty text")
        # End-if
        if not all(isinstance(value, str) for value in (role_prompt, toolset, context)):
            raise ValueError("role_prompt, toolset and context must be text")
        # End-if
        from src.core.subagent.registry import TOOLSET_REGISTRY
        if toolset not in TOOLSET_REGISTRY:
            raise ValueError("Unknown worker toolset")
        # End-if
        workspace = os.path.realpath(workspace)
        if not os.path.isdir(workspace):
            raise ValueError("workspace must be an existing directory")
        # End-if
        with self._lock:
            if self._closed:
                raise RuntimeError("JobManager is closed")
            # End-if
            self.start()
            job = self.store.create_job(session_id, workspace, task_description, role_prompt, toolset, context)
            self._schedule(job)
            return job
        # End-with
    # End-def

    ##
     # @brief Schedule one job with its workspace lock and cancellation event.
     #
     # @param job Persisted submission with its immutable context and workspace.
     #
     # @return None.
     #
    def _schedule(self, job):
        cancel = self._cancel.setdefault(job["id"], threading.Event())
        workspace_lock = self._workspaces.setdefault(job["workspace"], threading.Lock())
        future = self.pool.submit(self._run, job, cancel, workspace_lock)
        self._futures[job["id"]] = future
        future.add_done_callback(lambda done, job_id=job["id"]: self._forget(job_id))
    # End-def

    ##
     # @brief Release completed in-memory job tracking.
     #
     # @param job_id Persistent job identifier.
     #
     # @return None.
     #
    def _forget(self, job_id):
        with self._lock:
            self._futures.pop(job_id, None)
            self._cancel.pop(job_id, None)
        # End-with
    # End-def

    ##
     # @brief Run one job at a serialized workspace boundary.
     #
     # @param job Persisted submission with its immutable context and workspace.
     # @param cancel Cancellation event assigned to the job.
     # @param workspace_lock Lock serializing work in the target workspace.
     #
     # @return None.
     #
    def _run(self, job, cancel, workspace_lock):
        # A main agent has no filesystem execution tools. Workers that target
        # the same workspace run serially, while other workspaces may overlap.
        while not workspace_lock.acquire(timeout=0.1):
            if self._stopping.is_set():
                return
            # End-if
            if cancel.is_set():
                self.store.cancel_job(job["id"])
                return
            # End-if
        # End-while
        try:
            # Coordinate the queued -> running transition with close/cancel.
            # Once close owns this lock, a queued executor future cannot start
            # work even if it had already acquired its workspace lock.
            with self._lock:
                if self._closed or self._stopping.is_set():
                    return
                # End-if
                if cancel.is_set():
                    self.store.cancel_job(job["id"])
                    return
                # End-if
                if not self.store.start_job(job["id"]):
                    return
                # End-if
            # End-with
            try:
                result = self.executor(job, cancel)
                if not isinstance(result, dict):
                    raise TypeError("Job executor must return a result dictionary")
                # End-if
            except Exception as exc:
                result = {"status": "failed", "error_message": str(exc), "summary": "Worker failed."}
            # End-try
            if cancel.is_set():
                result = dict(result, status="cancelled")
            # End-if
            self.store.finish_job(job["id"], result)
        finally:
            workspace_lock.release()
        # End-try
    # End-def

    ##
     # @brief Read a job within an optional conversation scope.
     #
     # @param job_id Persistent job identifier.
     # @param session_id Conversation identifier used to scope persisted records.
     #
     # @return Scoped job dictionary, or None when inaccessible.
     #
    def get(self, job_id, session_id=None):
        job = self.store.get_job(job_id)
        if job is not None and session_id is not None and job["session_id"] != str(session_id):
            return None
        # End-if
        return job
    # End-def

    ##
     # @brief Persist cancellation and notify an active worker.
     #
     # @param job_id Persistent job identifier.
     # @param session_id Conversation identifier used to scope persisted records.
     #
     # @return True when the cancellation request was accepted.
     #
    def cancel(self, job_id, session_id=None):
        with self._lock:
            if self.get(job_id, session_id) is None:
                return False
            # End-if
            result = self.store.cancel_job(job_id)
            if result and job_id in self._cancel:
                self._cancel[job_id].set()
            # End-if
            return result
        # End-with
    # End-def

    ##
     # @brief Stop active work while retaining queued jobs for restart.
     #
     # @param wait Whether shutdown waits for active executor futures.
     #
     # @return None.
     #
    def close(self, wait=True):
        with self._lock:
            self._closed = True
            self._stopping.set()
            for job_id, cancel in self._cancel.items():
                if self.store.get_job(job_id)["status"] in ("running", "cancel_requested"):
                    self.store.cancel_job(job_id)
                    cancel.set()
                # End-if
            # End-for
        # End-with
        # Pending jobs stay queued in SQLite and can be recovered on restart.
        self.pool.shutdown(wait=wait, cancel_futures=True)
    # End-def
# End-class
