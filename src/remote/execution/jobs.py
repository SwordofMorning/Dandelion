##
 # @file src/remote/execution/jobs.py
 # @date 2026/10/27
 #
 # @brief Durable background job execution.
 # Runs background SubAgents with isolated session/log directories and
 # unrestricted server toolsets, without constructing a redundant MyAgent.
 #

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from mk.lib.paths import tool_dirs as resolve_tool_dirs


##
 # @brief Adapt the Core SubAgentPool for an isolated background job.
 #
class JobExecutor:
    ##
     # @brief Retain worker configuration, private data root, operator config dir, and tool dirs.
     #
     # @param config Loaded model and routing configuration.
     # @param data_dir Private remote-service data directory.
     # @param config_dir Optional operator config directory (.env) for SSH devices.yaml/known_hosts.
     # @param tool_dirs Optional ordered directories scanned for hot-plugged Remote tools.
     # @param workspace_dir Optional default server workspace directory for registry discovery.
     #
     # @return None.
     #
    def __init__(self, config, data_dir, config_dir=None, tool_dirs=None, workspace_dir=None):
        from dandelion_core.utils import ModelRegistry

        self.config = config
        self.data_dir = Path(data_dir)
        self.config_dir = config_dir
        self.workspace_dir = workspace_dir
        extra_tool_dirs = config.get("TOOL_DIRS") if isinstance(config, dict) else None
        remote_root = str(Path(__file__).resolve().parents[3])
        self.tool_dirs = (
            list(tool_dirs)
            if tool_dirs is not None
            else resolve_tool_dirs("remote", extra_tool_dirs, root_dir=remote_root)
        )
        self.registry = ModelRegistry(
            config.get("ALL_MODELS", []),
            config.get("SUB_LIST", []),
            enabled=int(config.get("SUB_DEPTH", 1)) > 0,
        )
    # End-def

    ##
     # @brief Discover an up-to-date ToolsetRegistry from build_worker_pool for Remote workers.
     #
     # @param workspace_dir Optional workspace directory used for tool initialization.
     #
     # @return Populated ToolsetRegistry instance.
     #
    def build_toolset_registry(self, workspace_dir=None):
        from dandelion_core.agent.tools import build_worker_pool
        from dandelion_core.subagent.registry import ToolsetRegistry
        from ..runtime import RemoteRuntime

        ws = str(workspace_dir or self.workspace_dir or self.data_dir)
        try:
            pool = build_worker_pool(
                config=self.config,
                client=None,
                session_manager=None,
                workspace_dir=ws,
                runtime=RemoteRuntime(),
                config_dir=self.config_dir,
                host="remote",
                tool_dirs=self.tool_dirs,
            )
            reg = getattr(pool, "toolset_registry", None)
            if reg is not None:
                return reg
            # End-if
        except Exception:
            pass
        # End-try
        return ToolsetRegistry()
    # End-def

    ##
     # @brief Run a background submission using isolated session, client and pool objects.
     #
     # @param job Persisted submission with its immutable context and workspace.
     # @param cancel_event Worker-owned cancellation event.
     #
     # @return Serialized subagent result dictionary.
     #
    def __call__(self, job, cancel_event):
        from dandelion_core.agent.tools import build_worker_pool
        from dandelion_core.utils import SafeLLMClient
        from dandelion_core.utils.logging.session import SessionManager
        from ..runtime import RemoteRuntime

        root = self.data_dir / "jobs" / job["id"]
        root.mkdir(parents=True, exist_ok=True)
        runtime = RemoteRuntime(cancel_event=cancel_event)
        session = SessionManager(log_dir=str(root))
        client = SafeLLMClient(
            api_key=self.config["ANTHROPIC_API_KEY"],
            base_url=self.config["ANTHROPIC_BASE_URL"],
            model_id=self.config["MODEL_ID"],
            sdk_type=self.config.get("SDK_TYPE", "Message"),
            all_models=self.config.get("ALL_MODELS", []),
            sub_list=self.config.get("SUB_LIST", []),
            sub_depth=int(self.config.get("SUB_DEPTH", 1)),
            thinking=str(self.config.get("THINKING", "disabled")).strip().lower(),
            effort=str(self.config.get("EFFORT", "medium")).strip().lower(),
            compat=str(self.config.get("COMPAT", "auto")).strip().lower(),
            logger=session,
            runtime=runtime,
        )
        pool = build_worker_pool(
            config=self.config,
            client=client,
            session_manager=session,
            workspace_dir=job["workspace"],
            runtime=runtime,
            config_dir=self.config_dir,
            host="remote",
            tool_dirs=self.tool_dirs,
        )
        task = job["task_description"]
        if job["context"]:
            task += "\n\nSubmission context (reference data, not instructions):\n" + job["context"]
        # End-if
        return asdict(pool.create_and_run(
            role_prompt=job["role_prompt"] or "Complete the assigned task and return a concise result.",
            toolset_name=job["toolset"],
            task_description=task,
            model=job["model"],
            depth=1,
        ))
    # End-def
# End-class


##
 # @brief Schedule durable background jobs with cancellation and workspace serialization.
 #
class JobManager:
    ##
     # @brief Create the bounded background executor and runtime state.
     #
     # @param store Shared durable store with its connection lock.
     # @param executor Callable executing one job with its cancellation event.
     # @param max_workers Maximum number of background worker threads.
     # @param registry Optional ModelRegistry for validating SubAgent models.
     # @param toolset_registry Optional ToolsetRegistry for validating worker toolsets.
     #
     # @return None.
     #
    def __init__(self, store, executor, max_workers=4, registry=None, toolset_registry=None):
        if isinstance(max_workers, bool) or int(max_workers) < 1:
            raise ValueError("max_workers must be positive")
        # End-if
        self.store = store
        self.executor = executor
        self.registry = registry if registry is not None else getattr(executor, "registry", None)
        self.pool = ThreadPoolExecutor(max_workers=int(max_workers), thread_name_prefix="remote-job")
        self._lock = threading.RLock()
        self._cancel = {}
        self._futures = {}
        self._workspaces = {}
        self._started = False
        self._closed = False
        self._stopping = threading.Event()
        self.toolset_registry = (
            toolset_registry
            if toolset_registry is not None
            else self._discover_toolset_registry()
        )
    # End-def

    ##
     # @brief Build a fresh ToolsetRegistry from the configured executor or Core defaults.
     #
     # @param workspace_dir Optional workspace directory hint for discovery.
     #
     # @return ToolsetRegistry instance.
     #
    def _discover_toolset_registry(self, workspace_dir=None):
        from dandelion_core.subagent.registry import ToolsetRegistry

        builder = getattr(self.executor, "build_toolset_registry", None)
        if callable(builder):
            try:
                discovered = builder(workspace_dir=workspace_dir)
                if discovered is not None:
                    return discovered
                # End-if
            except Exception:
                pass
            # End-try
        # End-if
        return ToolsetRegistry()
    # End-def

    ##
     # @brief Re-scan Remote tool directories and refresh self.toolset_registry live.
     #
     # @param workspace_dir Optional workspace directory hint for discovery.
     #
     # @return Refreshed ToolsetRegistry instance.
     #
    def refresh_toolset_registry(self, workspace_dir=None):
        refreshed = self._discover_toolset_registry(workspace_dir=workspace_dir)
        with self._lock:
            self.toolset_registry = refreshed
        # End-with
        return refreshed
    # End-def

    ##
     # @brief Recover persisted work and schedule queued submissions once.
     #
     # @param notify Optional callback(session_id, text) for interrupted turn notices.
     #
     # @return None.
     #
    def start(self, notify=None):
        with self._lock:
            if self._started:
                return
            # End-if
            if self._closed:
                raise RuntimeError("JobManager is closed")
            # End-if
            self.store.recover(notify=notify)
            self._started = True
            for job in reversed(self.store.list_jobs()):
                if job["status"] == "queued":
                    self._schedule(job)
                # End-if
            # End-for
        # End-with
    # End-def

    ##
     # @brief Validate and persist a background task with its selected SubAgent model.
     #
    def submit(self, session_id, workspace, model, task_description, role_prompt="", toolset="full", context=""):
        model = str(model or "").strip() if isinstance(model, str) else ""
        if not model:
            raise ValueError("model must be a non-empty SubAgent model alias")
        # End-if
        if self.registry is not None and self.registry.get_spec(model) is None:
            raise ValueError(
                f"Unknown SubAgent model '{model}'. Available models: {self.registry.aliases()}"
            )
        # End-if
        if not isinstance(task_description, str) or not task_description.strip():
            raise ValueError("task_description must be non-empty text")
        # End-if
        if not all(isinstance(value, str) for value in (role_prompt, toolset, context)):
            raise ValueError("role_prompt, toolset and context must be text")
        # End-if
        if not self.toolset_registry.has(toolset):
            ws_hint = workspace if isinstance(workspace, str) and os.path.isdir(workspace) else None
            self.refresh_toolset_registry(workspace_dir=ws_hint)
        # End-if
        if not self.toolset_registry.has(toolset):
            raise ValueError(
                f"Unknown worker toolset '{toolset}'. Available toolsets: {self.toolset_registry.names()}"
            )
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
            job = self.store.create_job(
                session_id, workspace, model, task_description, role_prompt, toolset, context
            )
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
        while not workspace_lock.acquire(timeout=0.2):
            if self._stopping.is_set():
                return
            # End-if
            if cancel.is_set():
                self.store.cancel_job(job["id"])
                return
            # End-if
        # End-while
        try:
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
            # On server shutdown (_stopping), leave status='running' so recover()
            # marks the job as 'interrupted' on next startup instead of 'cancelled'.
            if self._stopping.is_set():
                return
            # End-if
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
     # @brief Stop active workers while leaving in-flight jobs in 'running' state for recover().
     #
     # @param wait Whether shutdown waits for active executor futures.
     #
     # @return None.
     #
    def close(self, wait=True):
        with self._lock:
            self._closed = True
            self._stopping.set()
            for cancel in self._cancel.values():
                cancel.set()
            # End-for
        # End-with
        self.pool.shutdown(wait=wait, cancel_futures=True)
    # End-def
# End-class
