##
 # @file src/remote/tool/agent/task_tool.py
 # @date 2026/10/27
 #
 # @brief Remote main-agent delegation and schedule control tools.
 # Exposes non-blocking background job submission and job/schedule management
 # to the Dandelion Remote main agent.
 #

import json
from src.core.tool.base_tool import BaseTool
from src.core.subagent.registry import TOOLSET_REGISTRY


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
    def __init__(self, jobs, session_id, workspace, context, registry=None):
        super().__init__(workspace_dir=workspace)
        self.jobs, self.session_id, self.context = jobs, session_id, context
        self.registry = registry if registry is not None else getattr(jobs, "registry", None)
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
     # @brief Describe the tool capability and configured SubAgent models to the model.
     #
     # @return Human-readable tool description.
     #
    def get_description(self):
        base = (
            "Submit background SubAgent work on the selected 'model' and immediately receive a job_id "
            "so the foreground agent stays responsive for live conversation. Completion or failure "
            "arrives as a later interrupt event."
        )
        catalog = self.registry.format_catalog() if self.registry else ""
        if not catalog:
            return base
        # End-if
        return f"{base}\nAvailable SubAgent models:\n{catalog}"
    # End-def

    ##
     # @brief Return the model-visible JSON input schema.
     #
     # @return JSON schema dictionary.
     #
    def get_schema(self):
        aliases = self.registry.aliases() if self.registry else []
        model_prop = {
            "type": "string",
            "description": "Name of the SubAgent model to run this background job.",
        }
        if aliases:
            model_prop["enum"] = aliases
        # End-if
        return {
            "type": "object",
            "properties": {
                "model": model_prop,
                "task_description": {"type": "string"},
                "role_prompt": {"type": "string"},
                "toolset": {"type": "string", "enum": list(TOOLSET_REGISTRY)},
            },
            "required": ["model", "task_description"],
        }
    # End-def

    ##
     # @brief Execute one validated tool action and return its structured outcome.
     #
    def execute(self, model, task_description, role_prompt="", toolset="full"):
        try:
            job = self.jobs.submit(
                self.session_id,
                self.workspace_dir,
                model,
                task_description,
                role_prompt,
                toolset,
                self.context(),
            )
            return True, json.dumps({"accepted": True, "job_id": job["id"], "model": job["model"]})
        except (ValueError, RuntimeError) as exc:
            return False, str(exc)
        # End-try
    # End-def
# End-class


##
 # @brief Expose conversation background jobs and persistent schedules.
 #
class TaskControlTool(BaseTool):
    ##
     # @brief Bind the tool to its remote conversation and job manager.
     #
     # @param jobs Background job manager owning the shared durable store.
     # @param session_id Conversation identifier used to scope persisted records.
     #
     # @return None.
     #
    def __init__(self, jobs, session_id):
        super().__init__()
        self.jobs = jobs
        self.session_id = session_id
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
            "List/get/cancel this conversation's background jobs, or create/list/cancel persistent "
            "schedules. A reminder sends text directly without a model call; analysis wakes the main agent. "
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
                    ],
                },
                "job_id": {"type": "string"},
                "schedule_id": {"type": "string"},
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
     # @param action Requested control or schedule operation.
     # @param job_id Target background job identifier for get or cancel.
     # @param schedule_id Target schedule identifier for cancel_schedule.
     # @param due_at Timezone-aware ISO8601 timestamp for schedule creation.
     # @param text Reminder or analysis text for schedule creation.
     # @param kind Schedule delivery mode ("reminder" or "analysis").
     # @param interval Optional repeat interval in seconds.
     #
     # @return (success, payload_or_error) tuple.
     #
    def execute(self, action, job_id="", schedule_id="", due_at="", text="", kind="reminder", interval=None):
        try:
            if action == "list":
                result = [
                    {key: job[key] for key in ("id", "model", "status", "task_description", "created_at")}
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
            else:
                return False, "Unknown action"
            # End-if
            return True, json.dumps(result, ensure_ascii=False)
        except (ValueError, TypeError, KeyError) as exc:
            return False, str(exc)
        # End-try
    # End-def
# End-class
