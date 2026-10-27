"""Small remote-main toolset; actual work remains in background Core workers."""

import json
from src.tool.base_tool import BaseTool
from src.subagent.registry import TOOLSET_REGISTRY


class SubmitTaskTool(BaseTool):
    def __init__(self, jobs, session_id, workspace, context):
        super().__init__(workspace_dir=workspace)
        self.jobs, self.session_id, self.context = jobs, session_id, context

    def get_name(self):
        return "submit_task"

    def get_description(self):
        return "Submit background work and immediately receive a job_id. Completion arrives as a later event."

    def get_schema(self):
        return {"type": "object", "properties": {
            "task_description": {"type": "string"},
            "role_prompt": {"type": "string"},
            "toolset": {"type": "string", "enum": list(TOOLSET_REGISTRY)},
        }, "required": ["task_description"]}

    def execute(self, task_description, role_prompt="", toolset="full"):
        try:
            job = self.jobs.submit(self.session_id, self.workspace_dir, task_description,
                                   role_prompt, toolset, self.context())
            return True, json.dumps({"accepted": True, "job_id": job["id"]})
        except (ValueError, RuntimeError) as exc:
            return False, str(exc)


class TaskControlTool(BaseTool):
    def __init__(self, jobs, session_id):
        super().__init__()
        self.jobs, self.session_id = jobs, session_id

    def get_name(self):
        return "task_control"

    def get_description(self):
        return ("List/get/cancel this conversation's jobs, or create/list/cancel persistent schedules. "
                "A reminder sends text directly without a model call; analysis wakes the main agent. "
                "Use timezone-aware ISO8601 due_at and optional interval in seconds.")

    def get_schema(self):
        return {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "get", "cancel", "schedule", "list_schedules", "cancel_schedule"]},
            "job_id": {"type": "string"}, "schedule_id": {"type": "string"},
            "due_at": {"type": "string"}, "text": {"type": "string"},
            "kind": {"type": "string", "enum": ["reminder", "analysis"]},
            "interval": {"type": "number", "minimum": 1},
        }, "required": ["action"]}

    def execute(self, action, job_id="", schedule_id="", due_at="", text="", kind="reminder", interval=None):
        try:
            if action == "list":
                result = [{key: job[key] for key in ("id", "status", "task_description", "created_at")}
                          for job in self.jobs.store.list_jobs(self.session_id)[:50]]
            elif action == "get":
                result = self.jobs.get(job_id, self.session_id)
                if result is None:
                    return False, "Job not found in this conversation."
            elif action == "cancel":
                result = {"cancelled": self.jobs.cancel(job_id, self.session_id)}
            elif action == "schedule":
                if not text.strip():
                    raise ValueError("Schedule text is required")
                result = self.jobs.store.create_schedule(self.session_id, due_at, {"text": text}, interval, kind)
            elif action == "list_schedules":
                result = self.jobs.store.list_schedules(self.session_id)
            elif action == "cancel_schedule":
                result = {"cancelled": self.jobs.store.cancel_schedule(schedule_id, self.session_id)}
            else:
                return False, "Unknown action"
            return True, json.dumps(result, ensure_ascii=False)
        except (ValueError, TypeError) as exc:
            return False, str(exc)
