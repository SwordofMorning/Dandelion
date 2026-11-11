##
 # @file test/test_subagent_llm_decide.py
 # @date 2026/11/11
 #
 # @brief Unit tests for LLM-driven SubAgent model selection, strict PlanTool,
 # depth=2 nested delegation, per-model media isolation, and Remote job model persistence.
 #

from types import SimpleNamespace
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from src.core.utils.config import load_api_config
from src.core.utils.llm_request import SafeLLMClient, ModelRegistry
from src.core.utils.logging import SessionManager
from src.core.runtime import Runtime
from src.core.agent.skill import SkillManager
from src.core.agent.memory import MemoryManager
from src.core.agent.tools import _build_worker_catalog, build_worker_pool
from src.core.subagent import SubAgentPool
from src.core.tool.agent.plan_tool import PlanTool
from src.core.tool.agent.spawn_tool import SpawnSubagentTool
from src.remote.storage.store import Store
from src.remote.execution.jobs import JobExecutor, JobManager
from src.remote.tool.agent.task_tool import SubmitTaskTool, TaskControlTool


def _make_sample_models():
    return [
        {
            "provider_name": "SecA",
            "sdk_type": "Message",
            "base_url": "https://example.com/a",
            "api_key": "sk-a",
            "name": "opus",
            "model_id": "claude-opus-4-6",
            "compat": "anthropic",
            "description": "Strong text reasoning model for architecture design, refactoring, and code review.",
            "max_token": 16384,
            "max_context_tokens": 200000,
            "thinking": "enabled",
            "effort": "high",
            "file": [],
            "media_limits": {},
        },
        {
            "provider_name": "SecB",
            "sdk_type": "Chat Completions",
            "base_url": "https://example.com/b",
            "api_key": "sk-b",
            "name": "gemini",
            "model_id": "gemini-3-flash",
            "compat": "gemini",
            "description": "Fast multimodal model supporting PDF and image inspection plus lightweight edits.",
            "max_token": 65536,
            "max_context_tokens": 800000,
            "thinking": "enabled",
            "effort": "medium",
            "file": [".png", ".jpg", ".pdf"],
            "media_limits": {"max_media_per_request": 6},
        },
    ]
# End-def


class _FakeProvider:
    def __init__(self, name, responder):
        self.name = name
        self.responder = responder
        self.calls = []
    # End-def

    def safe_stream_request(self, payload, logger=None, log_tag="", echo=False):
        self.calls.append((dict(payload), log_tag, True))
        return self.responder(self.name, payload)
    # End-def

    def safe_request(self, payload, logger=None, log_tag=""):
        self.calls.append((dict(payload), log_tag, False))
        return self.responder(self.name, payload)
    # End-def

    @staticmethod
    def extract_text(content):
        if isinstance(content, str):
            return content
        # End-if
        parts = []
        for b in content or []:
            txt = b.get("text") if isinstance(b, dict) else getattr(b, "text", None)
            if txt:
                parts.append(txt)
            # End-if
        # End-for
        return "\n".join(parts)
    # End-def
# End-class


class TestSubAgentLlmDecide(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
    # End-def

    def tearDown(self):
        self._tmp.cleanup()
    # End-def

    def _write_cfg(self, text: str) -> str:
        p = self.tmp / "test.cfg"
        p.write_text(text, encoding="utf-8")
        return str(p)
    # End-def

    def test_config_strict_fail_fast_validation(self):
        # 1. Legacy basic.conditions must raise ValueError
        bad_cond = """[Main]
MAIN_AGENT = opus
SUB_LIST = ["opus"]
SUB_DEPTH = 1

[P]
SDK_TYPE = "Message"
BASE_URL = https://example.com
API_KEY = sk-test
MODEL_LIST = [{"provider": {"model_id": "m1"}, "basic": {"name": "opus", "conditions": ["complex"]}}]
"""
        with self.assertRaises(ValueError) as ctx1:
            load_api_config(self._write_cfg(bad_cond))
        # End-with
        self.assertIn("basic.conditions", str(ctx1.exception))

        # 2. Empty description on SUB_LIST model when SUB_DEPTH > 0 must raise ValueError
        empty_desc = """[Main]
MAIN_AGENT = opus
SUB_LIST = ["opus"]
SUB_DEPTH = 1

[P]
SDK_TYPE = "Message"
BASE_URL = https://example.com
API_KEY = sk-test
MODEL_LIST = [{"provider": {"model_id": "m1"}, "basic": {"name": "opus", "description": "   "}}]
"""
        with self.assertRaises(ValueError) as ctx2:
            load_api_config(self._write_cfg(empty_desc))
        # End-with
        self.assertIn("basic.description", str(ctx2.exception))

        # 3. Unknown alias in SUB_LIST must raise ValueError
        unknown_sub = """[Main]
MAIN_AGENT = opus
SUB_LIST = ["missing_model"]
SUB_DEPTH = 1

[P]
SDK_TYPE = "Message"
BASE_URL = https://example.com
API_KEY = sk-test
MODEL_LIST = [{"provider": {"model_id": "m1"}, "basic": {"name": "opus", "description": "Main model"}}]
"""
        with self.assertRaises(ValueError) as ctx3:
            load_api_config(self._write_cfg(unknown_sub))
        # End-with
        self.assertIn("missing_model", str(ctx3.exception))

        # 4. Valid config with UTF-8 description succeeds
        good_cfg = """[Main]
MAIN_AGENT = main_only
SUB_LIST = ["worker_a"]
SUB_DEPTH = 2

[P]
SDK_TYPE = "Message"
BASE_URL = https://example.com
API_KEY = sk-test
MODEL_LIST = [
    {"provider": {"model_id": "m-main"}, "basic": {"name": "main_only"}},
    {"provider": {"model_id": "m-sub"}, "basic": {"name": "worker_a", "description": "Reasoning worker model"}}
    ]
"""
        loaded = load_api_config(self._write_cfg(good_cfg))
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["SUB_LIST"], ["worker_a"])
        self.assertEqual(loaded["SUB_DEPTH"], 2)
    # End-def

    def test_plan_tool_strict_invariants(self):
        registry = ModelRegistry(_make_sample_models(), ["opus", "gemini"])
        plan_tool = PlanTool(registry=registry)

        schema = plan_tool.get_schema()
        sub_props = schema["properties"]["subtasks"]["items"]["properties"]
        self.assertEqual(sub_props["model"]["enum"], ["opus", "gemini"])

        # 1. Reject unknown model
        ok, msg = plan_tool.execute(
            overall_goal="Refactor",
            execution_strategy="parallel",
            subtasks=[{
                "id": "t1",
                "model": "gpt-unknown",
                "toolset": "minimal",
                "description": "Read files",
                "role_prompt": "Worker",
            }],
        )
        self.assertFalse(ok)
        self.assertIn("gpt-unknown", msg)

        # 2. Reject cycle in depends_on
        ok, msg = plan_tool.execute(
            overall_goal="Cycle test",
            execution_strategy="mixed",
            subtasks=[
                {
                    "id": "t1", "model": "opus", "toolset": "minimal",
                    "description": "Step 1", "role_prompt": "Worker", "depends_on": ["t2"],
                },
                {
                    "id": "t2", "model": "gemini", "toolset": "minimal",
                    "description": "Step 2", "role_prompt": "Worker", "depends_on": ["t1"],
                },
            ],
        )
        self.assertFalse(ok)
        self.assertIn("cycle", msg.lower())

        # 3. Reject parallel write conflict on normalized same file
        ok, msg = plan_tool.execute(
            overall_goal="Parallel file conflict",
            execution_strategy="parallel",
            subtasks=[
                {
                    "id": "t1", "model": "opus", "toolset": "filesystem",
                    "description": "Edit shared file", "role_prompt": "Worker",
                    "target_files": ["./src/shared.py"],
                },
                {
                    "id": "t2", "model": "gemini", "toolset": "filesystem",
                    "description": "Also edit shared file", "role_prompt": "Worker",
                    "target_files": ["src/shared.py"],
                },
            ],
        )
        self.assertFalse(ok)
        self.assertIn("src/shared.py", msg)

        # 4. Accept overlapping target_files when serialized via depends_on, computing waves
        ok, out = plan_tool.execute(
            overall_goal="Multi-wave plan",
            execution_strategy="mixed",
            subtasks=[
                {
                    "id": "t1", "model": "gemini", "toolset": "filesystem",
                    "description": "Inspect PDF and draft base", "role_prompt": "Worker",
                    "target_files": ["src/shared.py"],
                },
                {
                    "id": "t2", "model": "opus", "toolset": "code_analysis",
                    "description": "Audit independent module", "role_prompt": "Reviewer",
                    "target_files": [],
                },
                {
                    "id": "t3", "model": "opus", "toolset": "filesystem",
                    "description": "Finalize shared.py after t1", "role_prompt": "Refactorer",
                    "depends_on": ["t1"],
                    "target_files": ["src/shared.py"],
                },
            ],
        )
        self.assertTrue(ok, msg=out)
        parsed = json.loads(out)
        self.assertEqual(parsed["execution_waves"], [["t1", "t2"], ["t3"]])
    # End-def

    def test_subagent_request_no_fallback_and_max_tokens(self):
        providers = {}

        def fake_create(sdk_type, api_key, base_url, model_id, *args, **kwargs):
            def responder(name, payload):
                if model_id == "claude-opus-4-6":
                    return None, "HTTP 429 rate_limit_error"
                # End-if
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="gemini ok")],
                    stop_reason="end_turn",
                ), None
            # End-def
            p = _FakeProvider(model_id, responder)
            providers[model_id] = p
            return p
        # End-def

        with patch.object(SafeLLMClient, "_create_provider", side_effect=fake_create):
            client = SafeLLMClient(
                api_key="sk-main",
                base_url="https://example.com",
                model_id="main-model",
                sdk_type="Message",
                all_models=_make_sample_models(),
                sub_list=["opus", "gemini"],
                runtime=Runtime(echo=False),
            )

            # Failing opus must NOT fall back to gemini
            resp, err = client.subagent_request(
                "opus",
                {
                    "messages": [
                        {"role": "user", "content": "part 1"},
                        {"role": "user", "content": "part 2"},
                    ],
                    "max_tokens": 999999,
                    "system": "sys",
                },
                stream=True,
                echo=False,
            )
            self.assertIsNone(resp)
            self.assertIn("opus", err)
            self.assertIn("429", err)
            self.assertEqual(len(providers["claude-opus-4-6"].calls), 1)
            self.assertNotIn("gemini-3-flash", providers)

            # Verify message normalization and per-model max_tokens clamping (opus max_token=16384)
            sent_payload = providers["claude-opus-4-6"].calls[0][0]
            self.assertEqual(sent_payload["max_tokens"], 16384)
            self.assertEqual(len(sent_payload["messages"]), 1)

            # Explicit gemini call succeeds with gemini's max_tokens
            resp2, err2 = client.subagent_request(
                "gemini",
                {"messages": [{"role": "user", "content": "hi"}], "system": "sys"},
                stream=False,
            )
            self.assertIsNone(err2)
            self.assertIsNotNone(resp2)
            self.assertEqual(providers["gemini-3-flash"].calls[0][0]["max_tokens"], 65536)
        # End-with
    # End-def

    def test_depth2_plan_spawn_and_media_isolation(self):
        ws = self.tmp / "ws"
        (ws / "llm" / "skill").mkdir(parents=True)
        (ws / "llm" / "memory").mkdir(parents=True)
        sess = SessionManager(log_dir=str(self.tmp / "logs"))
        runtime = Runtime(output=lambda _: None, echo=False, sandbox=False)

        cfg = {
            "MAX_TOKENS": 128000,
            "SUB_DEPTH": 2,
            "SUB_CONCURRENCY": 2,
            "MEDIA_EXTS": [],
            "MEDIA_LIMITS": {},
            "ALL_MODELS": _make_sample_models(),
            "SUB_LIST": ["opus", "gemini"],
        }

        seen_subagent_schemas = {}

        def fake_create(sdk_type, api_key, base_url, model_id, *args, **kwargs):
            def responder(name, payload):
                tool_names = [t["name"] for t in payload.get("tools", [])]
                seen_subagent_schemas[model_id] = tool_names
                if model_id == "claude-opus-4-6" and len(payload["messages"]) == 1:
                    # Depth-1 text-only opus spawns depth-2 multimodal gemini
                    return SimpleNamespace(
                        content=[SimpleNamespace(
                            type="tool_use",
                            id="call_sub_1",
                            name="spawn_subagent",
                            input={
                                "model": "gemini",
                                "toolset": "filesystem",
                                "task_description": "Inspect schematic diagram",
                                "role_prompt": "Vision reader",
                            },
                        )],
                        stop_reason="tool_use",
                    ), None
                # End-if
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text=f"completed by {model_id}")],
                    stop_reason="end_turn",
                ), None
            # End-def
            return _FakeProvider(model_id, responder)
        # End-def

        with patch.object(SafeLLMClient, "_create_provider", side_effect=fake_create):
            client = SafeLLMClient(
                api_key="sk-main",
                base_url="https://example.com",
                model_id="main-text-only",
                sdk_type="Message",
                all_models=cfg["ALL_MODELS"],
                sub_list=cfg["SUB_LIST"],
                logger=sess,
                runtime=runtime,
            )
            skill_mgr = SkillManager(str(ws / "llm" / "skill"), runtime=runtime)
            mem_mgr = MemoryManager(str(ws / "llm" / "memory"), session_manager=sess, runtime=runtime)
            worker_tools, main_tools = _build_worker_catalog(
                str(ws), cfg, skill_mgr, sess, runtime, host=None, memory_manager=mem_mgr
            )
            # Text-only Main Agent does not mount read_pdf or read_image
            self.assertNotIn("read_pdf", main_tools)
            self.assertNotIn("read_image", main_tools)

            pool = SubAgentPool(client, sess, cfg, worker_tools, max_depth=2, runtime=runtime)
            spawn_tool = SpawnSubagentTool(pool)
            self.assertIn("- opus:", spawn_tool.get_description())
            self.assertIn("- gemini:", spawn_tool.get_description())
            self.assertEqual(
                spawn_tool.get_schema()["properties"]["model"]["enum"],
                ["opus", "gemini"],
            )

            ok, ctx_text = spawn_tool.execute(
                model="opus",
                toolset="full",
                task_description="Coordinate review and visual check",
                role_prompt="Lead background orchestrator",
            )
            self.assertTrue(ok, msg=ctx_text)
            self.assertIn("model: opus", ctx_text)

            # Depth-1 text-only opus had plan_tool + spawn_subagent, NO read_pdf/read_image
            self.assertIn("plan_tool", seen_subagent_schemas["claude-opus-4-6"])
            self.assertIn("spawn_subagent", seen_subagent_schemas["claude-opus-4-6"])
            self.assertNotIn("read_pdf", seen_subagent_schemas["claude-opus-4-6"])
            self.assertNotIn("read_image", seen_subagent_schemas["claude-opus-4-6"])

            # Depth-2 multimodal gemini received read_pdf + read_image, NO plan_tool/spawn_subagent
            self.assertIn("read_pdf", seen_subagent_schemas["gemini-3-flash"])
            self.assertIn("read_image", seen_subagent_schemas["gemini-3-flash"])
            self.assertNotIn("plan_tool", seen_subagent_schemas["gemini-3-flash"])
            self.assertNotIn("spawn_subagent", seen_subagent_schemas["gemini-3-flash"])
        # End-with
    # End-def

    def test_remote_submit_store_and_job_failure_propagation(self):
        # 1. Legacy SQLite schema without 'model' column must fail fast with RuntimeError
        legacy_db = self.tmp / "legacy.sqlite3"
        with sqlite3.connect(legacy_db) as conn:
            conn.execute(
                "CREATE TABLE jobs (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
                "workspace TEXT NOT NULL, task_description TEXT NOT NULL, "
                "role_prompt TEXT NOT NULL, toolset TEXT NOT NULL, context TEXT NOT NULL, "
                "status TEXT NOT NULL, result TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)"
            )
        # End-with
        with self.assertRaises(RuntimeError) as legacy_ctx:
            Store(str(legacy_db))
        # End-with
        self.assertIn("missing required column 'model'", str(legacy_ctx.exception))

        # 2. Fresh Store + JobManager + SubmitTaskTool persists model and propagates error
        ws = self.tmp / "remote_ws"
        (ws / "llm" / "skill").mkdir(parents=True)
        (ws / "llm" / "memory").mkdir(parents=True)
        cfg = {
            "ANTHROPIC_API_KEY": "sk-main",
            "ANTHROPIC_BASE_URL": "https://example.com",
            "MODEL_ID": "main-model",
            "SDK_TYPE": "Message",
            "MAX_TOKENS": 64000,
            "SUB_DEPTH": 2,
            "SUB_CONCURRENCY": 2,
            "ALL_MODELS": _make_sample_models(),
            "SUB_LIST": ["opus", "gemini"],
        }

        def fake_create(sdk_type, api_key, base_url, model_id, *args, **kwargs):
            return _FakeProvider(model_id, lambda n, p: (None, "upstream 503 unavailable"))
        # End-def

        store = Store(str(self.tmp / "remote.sqlite3"))
        try:
            with patch.object(SafeLLMClient, "_create_provider", side_effect=fake_create):
                executor = JobExecutor(cfg, str(self.tmp / "remote_data"))
                jobs = JobManager(store, executor, max_workers=1)
                try:
                    submit = SubmitTaskTool(jobs, "telegram:100", str(ws), lambda: "")
                    self.assertIn("- opus:", submit.get_description())
                    self.assertEqual(
                        submit.get_schema()["properties"]["model"]["enum"],
                        ["opus", "gemini"],
                    )

                    # Reject unknown model immediately
                    bad_ok, bad_err = submit.execute(
                        model="nonexistent",
                        task_description="Run suite",
                    )
                    self.assertFalse(bad_ok)
                    self.assertIn("nonexistent", bad_err)

                    # Valid submission runs background job and emits job_done with model & error
                    ok, payload_json = submit.execute(
                        model="opus",
                        task_description="Run heavy background audit",
                        toolset="code_analysis",
                    )
                    self.assertTrue(ok, msg=payload_json)
                    job_id = json.loads(payload_json)["job_id"]

                    with store.changed:
                        store.changed.wait_for(
                            lambda: (store.get_job(job_id) or {}).get("status") == "failed",
                            timeout=5.0,
                        )
                    # End-with

                    ctrl = TaskControlTool(jobs, "telegram:100")
                    list_ok, list_json = ctrl.execute(action="list")
                    self.assertTrue(list_ok)
                    listed = json.loads(list_json)
                    self.assertEqual(listed[0]["model"], "opus")
                    self.assertEqual(listed[0]["status"], "failed")

                    ev = store.claim_event()
                    self.assertIsNotNone(ev)
                    self.assertEqual(ev["kind"], "job_done")
                    self.assertEqual(ev["payload"]["model"], "opus")
                    self.assertEqual(ev["payload"]["status"], "failed")
                    self.assertIn("503", ev["payload"]["result"]["error_message"])
                finally:
                    jobs.close()
                # End-try
            # End-with
        finally:
            store.close()
        # End-try
    # End-def

    def test_cli_and_remote_agent_tool_assembly_and_fallback_template(self):
        from src.core.agent import MyAgent
        from src.cli.sysprompt import CLIPromptBuilder
        from src.remote import cli as remote_cli
        from src.remote.execution.agent import make_agent_factory

        # 1. Verify src/remote/cli.py _FALLBACK_CFG_TEMPLATE loads cleanly under strict config rules
        rendered_cfg = self._write_cfg(remote_cli._FALLBACK_CFG_TEMPLATE)
        parsed_tpl = load_api_config(rendered_cfg)
        self.assertIsNotNone(parsed_tpl)
        self.assertEqual(parsed_tpl["SUB_DEPTH"], 2)
        self.assertEqual(parsed_tpl["SUB_LIST"], ["gemini"])

        # 2. Verify CLI MyAgent and Remote MyAgent tool wiring
        ws = self.tmp / "agent_ws"
        (ws / "llm" / "skill").mkdir(parents=True)
        (ws / "llm" / "memory").mkdir(parents=True)
        cfg = {
            "ANTHROPIC_API_KEY": "sk-main",
            "ANTHROPIC_BASE_URL": "https://example.com",
            "MODEL_ID": "main-model",
            "SDK_TYPE": "Message",
            "MAX_TOKENS": 64000,
            "MAX_CONTEXT_TOKENS": 200000,
            "SUB_DEPTH": 2,
            "SUB_CONCURRENCY": 2,
            "ALL_MODELS": _make_sample_models(),
            "SUB_LIST": ["opus", "gemini"],
            "MEDIA_EXTS": [],
            "MEDIA_LIMITS": {},
        }

        def fake_create(sdk_type, api_key, base_url, model_id, *args, **kwargs):
            return _FakeProvider(model_id, lambda n, p: (None, "unused"))
        # End-def

        with patch.object(SafeLLMClient, "_create_provider", side_effect=fake_create):
            cli_sess = SessionManager(log_dir=str(self.tmp / "cli_logs"))
            cli_agent = MyAgent(
                config=cfg,
                session_manager=cli_sess,
                workspace_dir=str(ws),
                runtime=Runtime(output=lambda _: None, echo=False),
                prompt_builder_cls=CLIPromptBuilder,
            )
            self.assertIn("plan_tool", cli_agent.tools)
            self.assertIn("spawn_subagent", cli_agent.tools)
            self.assertIn("Let LLM Decide", cli_agent.prompt_builder.build())

            store = Store(str(self.tmp / "agent_remote.sqlite3"))
            try:
                jobs = JobManager(store, JobExecutor(cfg, str(self.tmp / "rdata")), max_workers=1)
                try:
                    factory = make_agent_factory(cfg, str(self.tmp / "rdata"), str(ws), jobs)
                    remote_agent = factory("telegram:42")
                    self.assertIn("plan_tool", remote_agent.tools)
                    self.assertIn("submit_task", remote_agent.tools)
                    self.assertNotIn("spawn_subagent", remote_agent.tools)
                finally:
                    jobs.close()
                # End-try
            finally:
                store.close()
            # End-try
        # End-with
    # End-def
# End-class


if __name__ == "__main__":
    unittest.main()
# End-if
