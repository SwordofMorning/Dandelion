##
 # @file src/remote/execution/tools.py
 # @date 2026/10/27
 #
 # @brief Remote main-agent delegation and schedule/sync inspection tools.
 # Exposes non-blocking background job submission, job and schedule control,
 # and bounded reads of synced sessions in assets/syncs/.
 #

import json
from src.core.tool.base_tool import BaseTool
from src.core.subagent.registry import TOOLSET_REGISTRY
from ..storage.backups import BackupStore


##
 # @brief Submit non-blocking background work from the remote main agent.
 #
class SubmitTaskTool(BaseTool):
    ##
     # @brief Bind the tool to its remote conversation and job manager.
     #
     # @param jobs Background job manager used by remote delegation tools.
     # @param session_id Conversation identifier used to scope persisted records.
     # @param workspace Workspace assigned to the task or remote session.
     # @param context Callable returning an immutable snapshot of recent history.
     #
     # @return None.
     #
    def __init__(self, jobs, session_id, workspace, context):
        super().__init__(workspace_dir=workspace)
        self.jobs, self.session_id, self.context = jobs, session_id, context
    # End-def

    ##
     # @brief Return the registered tool name.
     #
     # @return Tool identifier string.
     #
    def get_name(self):
        return "submit_task"
    # End-def

    ##
     # @brief Describe the tool capability to the model.
     #
     # @return Human-readable tool description.
     #
    def get_description(self):
        return (
            "Submit background SubAgent work and immediately receive a job_id so the main agent "
            "stays responsive for live conversation. Completion arrives as a later interrupt event."
        )
    # End-def

    ##
     # @brief Return the model-visible JSON input schema.
     #
     # @return JSON schema dictionary.
     #
    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "task_description": {"type": "string"},
                "role_prompt": {"type": "string"},
                "toolset": {"type": "string", "enum": list(TOOLSET_REGISTRY)},
            },
            "required": ["task_description"],
        }
    # End-def

    ##
     # @brief Execute one validated tool action and return its structured outcome.
     #
     # @param task_description Nonempty natural-language task description.
     # @param role_prompt System role for the delegated worker.
     # @param toolset Registered worker toolset name.
     #
     # @return (success, payload_or_error) tuple.
     #
    def execute(self, task_description, role_prompt="", toolset="full"):
        try:
            job = self.jobs.submit(
                self.session_id,
                self.workspace_dir,
                task_description,
                role_prompt,
                toolset,
                self.context(),
            )
            return True, json.dumps({"accepted": True, "job_id": job["id"]})
        except (ValueError, RuntimeError) as exc:
            return False, str(exc)
        # End-try
    # End-def
# End-class


##
 # @brief Expose conversation jobs, schedules and synced sessions in assets/syncs/.
 #
class TaskControlTool(BaseTool):
    ##
     # @brief Bind the tool to its remote conversation, job manager and shared BackupStore.
     #
     # @param jobs Background job manager owning the shared durable store.
     # @param session_id Conversation identifier used to scope persisted records.
     # @param backups Optional shared BackupStore instance.
     #
     # @return None.
     #
    def __init__(self, jobs, session_id, backups=None):
        super().__init__()
        self.jobs = jobs
        self.session_id = session_id
        self.backups = backups if backups is not None else BackupStore(jobs.store)
    # End-def

    ##
     # @brief Return the registered tool name.
     #
     # @return Tool identifier string.
     #
    def get_name(self):
        return "task_control"
    # End-def

    ##
     # @brief Describe the tool capability to the model.
     #
     # @return Human-readable tool description.
     #
    def get_description(self):
        return (
            "List/get/cancel this conversation's background jobs, create/list/cancel persistent "
            "schedules, or inspect synced sessions in assets/syncs/ (list_sessions, read_session). "
            "A reminder sends text directly without a model call; analysis wakes the main agent. "
            "Use timezone-aware ISO8601 due_at and optional interval in seconds."
        )
    # End-def

    ##
     # @brief Return the model-visible JSON input schema.
     #
     # @return JSON schema dictionary.
     #
    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list",
                        "get",
                        "cancel",
                        "schedule",
                        "list_schedules",
                        "cancel_schedule",
                        "list_sessions",
                        "read_session",
                    ],
                },
                "job_id": {"type": "string"},
                "schedule_id": {"type": "string"},
                "snapshot_id": {"type": "string"},
                "device": {"type": "string"},
                "project": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "due_at": {"type": "string"},
                "text": {"type": "string"},
                "kind": {"type": "string", "enum": ["reminder", "analysis"]},
                "interval": {"type": "number", "minimum": 1},
            },
            "required": ["action"],
        }
    # End-def

    ##
     # @brief Execute one validated tool action and return its structured outcome.
     #
     # @param action Requested control or inspection operation.
     # @param job_id Target background job identifier for get or cancel.
     # @param schedule_id Target schedule identifier for cancel_schedule.
     # @param due_at Timezone-aware ISO8601 timestamp for schedule creation.
     # @param text Reminder or analysis text for schedule creation.
     # @param kind Schedule delivery mode ("reminder" or "analysis").
     # @param interval Optional repeat interval in seconds.
     # @param snapshot_id Target session_id or snapshot_id for read_session.
     # @param device Optional device filter for list_sessions.
     # @param project Optional project filter for list_sessions.
     # @param offset Starting message index for read_session pagination.
     # @param limit Maximum number of messages returned by read_session.
     #
     # @return (success, payload_or_error) tuple.
     #
    def execute(self, action, job_id="", schedule_id="", due_at="", text="", kind="reminder", interval=None,
                snapshot_id="", device=None, project=None, offset=0, limit=20):
        try:
            if action == "list":
                result = [
                    {key: job[key] for key in ("id", "status", "task_description", "created_at")}
                    for job in self.jobs.store.list_jobs(self.session_id)[:50]
                ]
            elif action == "get":
                result = self.jobs.get(job_id, self.session_id)
                if result is None:
                    return False, "Job not found in this conversation."
                # End-if
            elif action == "cancel":
                result = {"cancelled": self.jobs.cancel(job_id, self.session_id)}
            elif action == "schedule":
                if not text.strip():
                    raise ValueError("Schedule text is required")
                # End-if
                result = self.jobs.store.create_schedule(self.session_id, due_at, {"text": text}, interval, kind)
            elif action == "list_schedules":
                result = self.jobs.store.list_schedules(self.session_id)
            elif action == "cancel_schedule":
                result = {"cancelled": self.jobs.store.cancel_schedule(schedule_id, self.session_id)}
            elif action in ("list_sessions", "read_session"):
                if action == "list_sessions":
                    result = {"sessions": self.backups.list_sessions(device=device, project=project)}
                else:
                    result = self.backups.read_session(snapshot_id, offset=offset, limit=limit)
                # End-if
                encoded = json.dumps(result, ensure_ascii=False)
                if len(encoded) > 7400:
                    encoded = encoded[:7200] + (
                        "\n[Backup reference view truncated. Request a smaller history page or filter "
                        "list_sessions by device/project.]"
                    )
                # End-if
                return True, encoded
            else:
                return False, "Unknown action"
            # End-if
            return True, json.dumps(result, ensure_ascii=False)
        except (ValueError, TypeError, KeyError) as exc:
            return False, str(exc)
        # End-try
    # End-def
# End-class
