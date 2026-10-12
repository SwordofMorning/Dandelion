#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from src.core.compress import (
    ContextCompressor,
    is_context_overflow_error,
    extract_usage_tokens,
    classify_compaction_summary,
    build_compaction_message,
    msg_ends_with_tool_use,
    msg_char_length,
)


class DummyResponse:
    def __init__(self, content="", stop_reason="end_turn", usage=None):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage


class DummyClient:
    def __init__(self, return_text="<goals>Test Goals</goals><completed>Step 1</completed><decisions>None</decisions><artifacts>None</artifacts><pending>Step 2</pending>"):
        self.return_text = return_text
        self.last_payload = None

    def safe_request(self, payload, log_tag=""):
        self.last_payload = payload
        return DummyResponse(content=self.return_text, stop_reason="end_turn"), None

    def extract_text(self, content):
        return str(content)


class DummySession:
    def __init__(self, temp_dir):
        self.current_session_dir = temp_dir
        self.logs = []

    def _default_serializer(self, obj):
        return str(obj)

    def log_api_call(self, tag, payload):
        self.logs.append((tag, payload))

    def save_history(self, history):
        pass


class TestContextCompression(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.session = DummySession(self.temp_dir.name)
        self.client = DummyClient()
        self.config = {
            "MAX_TOKENS": 4096,
            "MAX_CONTEXT_TOKENS": 128000,
            "COMPACT_HEAD_SIZE": 2,
            "COMPACT_RECENT_SIZE": 4,
            "COMPACT_TARGET_REDUCTION": 0.20,
        }
        self.compressor = ContextCompressor(self.client, self.session, self.config)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_overflow_error_detection(self):
        # Anthropic patterns
        self.assertTrue(is_context_overflow_error("prompt is too long: 205000 tokens > 200000 maximum tokens"))
        self.assertTrue(is_context_overflow_error("Error: prompt_too_long"))

        # OpenAI patterns
        self.assertTrue(is_context_overflow_error("This model's maximum context length is 128000 tokens. However, your messages resulted in 135000 tokens"))
        self.assertTrue(is_context_overflow_error("Error code: context_length_exceeded"))

        # Gemini patterns
        self.assertTrue(is_context_overflow_error("ResourceExhausted: token count exceeds maximum"))
        self.assertTrue(is_context_overflow_error("context window exceeded"))
        self.assertTrue(is_context_overflow_error("request contains too many tokens"))

        # Non-overflow errors must return False
        self.assertFalse(is_context_overflow_error("429 Too Many Requests: Rate limit exceeded"))
        self.assertFalse(is_context_overflow_error("500 Internal Server Error"))
        self.assertFalse(is_context_overflow_error("Connection timed out"))
        self.assertFalse(is_context_overflow_error(""))
        self.assertFalse(is_context_overflow_error(None))

    def test_usage_tokens_extraction(self):
        # Anthropic usage object
        class AnthropicUsage:
            input_tokens = 4514
            output_tokens = 628
            cache_read_input_tokens = 100
            cache_creation_input_tokens = 50

        u1 = extract_usage_tokens(AnthropicUsage())
        self.assertEqual(u1["input"], 4514)
        self.assertEqual(u1["output"], 628)
        self.assertEqual(u1["cached"], 150)
        self.assertEqual(u1["total"], 4514 + 628 + 100 + 50)

        # Output format string verification
        display_str = f"Token: {u1['total']} | Input: {u1['input']} | Output: {u1['output']} | Cached: {u1['cached']}"
        self.assertEqual(display_str, "Token: 5292 | Input: 4514 | Output: 628 | Cached: 150")

        # OpenAI usage object
        class OpenAIDetails:
            cached_tokens = 80

        class OpenAIUsage:
            prompt_tokens = 2000
            completion_tokens = 500
            total_tokens = 2500
            prompt_tokens_details = OpenAIDetails()

        u2 = extract_usage_tokens(OpenAIUsage())
        self.assertEqual(u2["input"], 2000)
        self.assertEqual(u2["output"], 500)
        self.assertEqual(u2["cached"], 80)
        self.assertEqual(u2["total"], 2500)

        # None usage
        u3 = extract_usage_tokens(None)
        self.assertEqual(u3["total"], 0)
        self.assertEqual(u3["cached"], 0)

    def test_tool_use_pair_safety(self):
        assistant_tool = {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I will run bash command"},
                {"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": "ls"}}
            ]
        }
        user_result = {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "file1.txt\nfile2.txt"}
            ]
        }
        self.assertTrue(msg_ends_with_tool_use(assistant_tool))
        self.assertFalse(msg_ends_with_tool_use(user_result))

        # Check that cut index includes user_result if assistant_tool is included
        candidates = [assistant_tool, user_result]
        cut = self.compressor._find_safe_cut_index(candidates, target_chars=10)
        self.assertEqual(cut, 2)  # Both must be included together

    def test_smooth_80_percent_compression(self):
        # Build conversation: 2 head + 10 middle + 4 recent = 16 messages
        history = [
            {"role": "user", "content": "Initial user task requirement: create system architecture"},
            {"role": "assistant", "content": "Understood, starting project design."},
        ]

        # 10 middle turns of work
        for i in range(1, 6):
            history.append({
                "role": "assistant",
                "content": [
                    {"type": "text", "text": f"Running step {i}"},
                    {"type": "tool_use", "id": f"call_{i}", "name": "write_file", "input": {"path": f"f{i}.py"}}
                ]
            })
            history.append({
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": f"call_{i}", "content": f"Successfully wrote f{i}.py"}
                ]
            })

        # 4 recent turns
        for i in range(2):
            history.append({"role": "user", "content": f"Recent query {i}"})
            history.append({"role": "assistant", "content": f"Recent reply {i}"})

        orig_len = len(history)
        self.assertEqual(orig_len, 16)

        # Perform compression
        new_history, stats = self.compressor.compress(history, target_reduction=0.20)
        self.assertIsNotNone(new_history)
        self.assertEqual(stats["status"], "ok")

        # Head preserved
        self.assertEqual(new_history[0]["content"], history[0]["content"])
        self.assertEqual(new_history[1]["content"], history[1]["content"])

        # Summary message present after head
        summary_msg = new_history[2]
        self.assertEqual(summary_msg["role"], "user")
        self.assertTrue(summary_msg["content"].startswith("[System: Context compacted at"))

        # Recent messages preserved at tail
        self.assertEqual(new_history[-1]["content"], history[-1]["content"])
        self.assertEqual(new_history[-2]["content"], history[-2]["content"])

        # New history length must be shorter than original, but retain majority (~80%)
        self.assertLess(len(new_history), orig_len)
        self.assertGreater(len(new_history), orig_len * 0.5)

        # Archive file must exist
        self.assertTrue(os.path.exists(stats["archive_path"]))
        with open(stats["archive_path"], "r", encoding="utf-8") as f:
            archived = json.load(f)
        self.assertEqual(len(archived), orig_len)

    def test_incremental_summary_rolling(self):
        # History already contains a previous compaction summary
        prev_summary = "[System: Context compacted at /fake/path.json]\n\n<goals>Previous Goals</goals>"
        history = [
            {"role": "user", "content": "Initial prompt"},
            {"role": "assistant", "content": "Initial ack"},
            {"role": "user", "content": prev_summary},
        ]
        for i in range(8):
            history.append({"role": "user", "content": f"Middle message {i} with some detailed text"})
            history.append({"role": "assistant", "content": f"Middle reply {i} with some response text"})

        # Recent turns
        history.append({"role": "user", "content": "Recent question"})
        history.append({"role": "assistant", "content": "Recent answer"})

        new_history, stats = self.compressor.compress(history, target_reduction=0.25)
        self.assertIsNotNone(new_history)

        # Check payload sent to LLM included previous summary for rolling merge
        payload = self.client.last_payload
        self.assertIsNotNone(payload)
        user_prompt = payload["messages"][0]["content"]
        self.assertIn("Previous Summary:", user_prompt)
        self.assertIn("<goals>Previous Goals</goals>", user_prompt)


    def test_media_tool_result_dispatch_in_agent(self):
        # Verify that handling a media tool result does not reference any unbound variables
        from src.core.agent import MyAgent

        # Mock tool returning media dict
        class MockMediaTool:
            def execute(self, **kwargs):
                return True, {
                    "kind": "image",
                    "media_cost": 620,
                    "summary": "Loaded image (0.09 MB)",
                    "pointer": "[Multimodal asset: test.png cost=620 tokens]",
                    "block": {"type": "image", "source": {"type": "base64", "data": "abc"}}
                }
            def get_name(self):
                return "read_image"

        class Block:
            type = "tool_use"
            name = "read_image"
            id = "call_test_123"
            input = {"path": "test.png"}

        class StepResponse:
            content = [Block()]
            stop_reason = "tool_use"
            usage = None

        class MockClient:
            def safe_stream_request(self, payload, log_tag=""):
                return StepResponse(), None

        agent = MyAgent.__new__(MyAgent)
        agent.client = MockClient()
        agent.session = self.session
        agent.config = self.config
        agent.compressor = self.compressor
        agent._if_compress = self.compressor.condition
        agent.prompt_builder = type("MockPB", (), {"build": lambda self: "sys"})()
        agent.tool_schemas = []
        agent.tools = {"read_image": MockMediaTool()}
        agent.history = [{"role": "user", "content": "read image"}]
        agent._pending_media_blocks = []
        agent._is_plain_user_msg = lambda msg: True
        agent._inject_dynamic_context = lambda: None
        agent._invalidate_memories_cache = lambda: None

        cont, err = agent.step()
        self.assertTrue(cont)
        self.assertIsNone(err)
        self.assertEqual(len(agent._pending_media_blocks), 1)
        self.assertEqual(agent.history[-1]["role"], "user")

    def test_compress_condition_evaluation(self):
        from src.core.compress import CompressCondition

        cond = CompressCondition(max_context_tokens=79200)

        # 1. Total tokens exceeds threshold -> must compress
        self.assertTrue(cond(err=None, total_tokens=85023))

        # 2. Total tokens below threshold -> no compression
        self.assertFalse(cond(err=None, total_tokens=60000))
        self.assertFalse(cond(err=None, total_tokens=79200))

        # 3. Upstream error with overflow keywords -> must compress
        self.assertTrue(cond(err="prompt is too long: 200k", total_tokens=0))
        self.assertTrue(cond(err="context_length_exceeded", total_tokens=1000))

        # 4. Other API errors -> no compression
        self.assertFalse(cond(err="500 Internal Server Error", total_tokens=1000))

    def test_step_triggers_compaction_on_token_threshold(self):
        from src.core.agent import MyAgent

        # Simulated response: first attempt returns 85023 tokens (> 79200)
        # Second attempt (after compaction) returns 60000 tokens (< 79200)
        call_count = 0

        class ThresholdUsage:
            def __init__(self, total):
                self.input_tokens = total - 100
                self.output_tokens = 100
                self.cache_read_input_tokens = 0
                self.total_tokens = total

        class TextBlock:
            type = "text"
            text = "Answer"

        class StepTextResponse:
            def __init__(self, tokens):
                self.content = [TextBlock()]
                self.stop_reason = "end_turn"
                self.usage = ThresholdUsage(tokens)

        class MockThresholdClient:
            def safe_stream_request(self, payload, log_tag=""):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return StepTextResponse(85023), None
                else:
                    return StepTextResponse(60000), None

            def safe_request(self, payload, log_tag=""):
                return DummyResponse(
                    content="<goals>Updated Goals</goals><completed>Completed Step 1</completed><decisions>None</decisions>",
                    stop_reason="end_turn"
                ), None

            def extract_text(self, content):
                return str(content)

        # Build agent with 79200 threshold and long history to allow compaction
        config = {
            "MAX_TOKENS": 4096,
            "MAX_CONTEXT_TOKENS": 79200,
            "COMPACT_HEAD_SIZE": 2,
            "COMPACT_RECENT_SIZE": 2,
            "COMPACT_TARGET_REDUCTION": 0.20,
        }
        client = MockThresholdClient()
        compressor = ContextCompressor(client, self.session, config)

        agent = MyAgent.__new__(MyAgent)
        agent.client = client
        agent.session = self.session
        agent.config = config
        agent.compressor = compressor
        agent._if_compress = compressor.condition
        agent.prompt_builder = type("MockPB", (), {"build": lambda self: "sys"})()
        agent.tool_schemas = []
        agent.tools = {}
        # 10 messages so compaction can succeed
        agent.history = [
            {"role": "user", "content": "Goal"},
            {"role": "assistant", "content": "Ack"},
        ]
        for i in range(6):
            agent.history.append({"role": "user", "content": f"Message {i} " * 50})
            agent.history.append({"role": "assistant", "content": f"Reply {i} " * 50})
        agent.history.append({"role": "user", "content": "Final query"})
        agent._pending_media_blocks = []
        agent._is_plain_user_msg = lambda msg: True
        agent._inject_dynamic_context = lambda: None
        agent._invalidate_memories_cache = lambda: None

        cont, err = agent.step()
        self.assertFalse(cont)
        self.assertIsNone(err)
        # Verify it preserved resp without retrying, and compacted post-response
        self.assertEqual(call_count, 1)
        self.assertTrue(any(isinstance(m.get("content"), str) and "[System: Context compacted at" in m["content"] for m in agent.history))

    def test_step_retries_immediately_on_overflow_error(self):
        from src.core.agent import MyAgent

        call_count = 0

        class TextBlock:
            type = "text"
            text = "Recovered Answer"

        class StepTextResponse:
            def __init__(self):
                self.content = [TextBlock()]
                self.stop_reason = "end_turn"
                self.usage = None

        class MockOverflowClient:
            def safe_stream_request(self, payload, log_tag=""):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return None, "prompt is too long: 200000 tokens > 100000 maximum tokens"
                return StepTextResponse(), None

            def safe_request(self, payload, log_tag=""):
                return DummyResponse(
                    content="<goals>Recovered Goals</goals><completed>Completed Step 1</completed><decisions>None</decisions>",
                    stop_reason="end_turn"
                ), None

            def extract_text(self, content):
                return str(content)

        config = {
            "MAX_TOKENS": 4096,
            "MAX_CONTEXT_TOKENS": 100000,
            "COMPACT_HEAD_SIZE": 2,
            "COMPACT_RECENT_SIZE": 2,
            "COMPACT_TARGET_REDUCTION": 0.20,
        }
        client = MockOverflowClient()
        compressor = ContextCompressor(client, self.session, config)

        agent = MyAgent.__new__(MyAgent)
        agent.client = client
        agent.session = self.session
        agent.config = config
        agent.compressor = compressor
        agent._if_compress = compressor.condition
        agent.prompt_builder = type("MockPB", (), {"build": lambda self: "sys"})()
        agent.tool_schemas = []
        agent.tools = {}
        agent.history = [
            {"role": "user", "content": "Goal"},
            {"role": "assistant", "content": "Ack"},
        ]
        for i in range(6):
            agent.history.append({"role": "user", "content": f"Message {i} " * 50})
            agent.history.append({"role": "assistant", "content": f"Reply {i} " * 50})
        agent.history.append({"role": "user", "content": "Final query"})
        agent._pending_media_blocks = []
        agent._is_plain_user_msg = lambda msg: True
        agent._inject_dynamic_context = lambda: None
        agent._invalidate_memories_cache = lambda: None

        cont, err = agent.step()
        self.assertFalse(cont)
        self.assertIsNone(err)
        # Because overflow error occurred on attempt 1, it compacted and retried immediately
        self.assertEqual(call_count, 2)

    def test_usage_tokens_includes_cache_create(self):
        class AnthropicCacheCreateUsage:
            input_tokens = 1000
            output_tokens = 200
            cache_read_input_tokens = 300
            cache_creation_input_tokens = 500
            total_tokens = None

        u = extract_usage_tokens(AnthropicCacheCreateUsage())
        self.assertEqual(u["input"], 1000)
        self.assertEqual(u["output"], 200)
        self.assertEqual(u["cached"], 300 + 500)
        # total must include cache_create: 1000 + 200 + 300 + 500 = 2000
        self.assertEqual(u["total"], 2000)

    def test_compress_failure_preserves_history(self):
        # When LLM summary fails with empty or error status, compress must return None
        # and NOT truncate history
        class FailingSummaryClient:
            def safe_request(self, payload, log_tag=""):
                return None, "500 Internal Server Error"
            def extract_text(self, content):
                return ""

        compressor = ContextCompressor(FailingSummaryClient(), self.session, self.config)
        history = [
            {"role": "user", "content": "Goal"},
            {"role": "assistant", "content": "Ack"},
        ]
        for i in range(8):
            history.append({"role": "user", "content": f"User {i}"})
            history.append({"role": "assistant", "content": f"Bot {i}"})

        orig_len = len(history)
        new_history, stats = compressor.compress(history)
        self.assertIsNone(new_history)
        self.assertEqual(stats["status"], "error")
        self.assertEqual(len(history), orig_len)

    def test_compress_safe_recent_boundary_preserves_tool_pair(self):
        # Construct history where recent boundary would land on tool_result
        history = [
            {"role": "user", "content": "Head user"},
            {"role": "assistant", "content": "Head assistant"},
        ]
        for i in range(4):
            history.append({"role": "user", "content": f"Mid user {i}"})
            history.append({"role": "assistant", "content": f"Mid bot {i}"})

        # Add a tool call right at the recent boundary
        history.append({
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t_boundary", "name": "bash", "input": {"command": "ls"}}]
        })
        history.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t_boundary", "content": "ok"}]
        })
        # Followed by recent messages
        for i in range(4):
            history.append({"role": "user", "content": f"Recent user {i}"})
            history.append({"role": "assistant", "content": f"Recent bot {i}"})

        new_history, stats = self.compressor.compress(history, target_reduction=0.20)
        self.assertIsNotNone(new_history)
        self.assertEqual(stats["status"], "ok")

        # Verify no tool_result is ever detached from its tool_use
        for i, msg in enumerate(new_history):
            content = msg.get("content")
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                # Preceding message must be assistant with tool_use
                self.assertGreater(i, 0)
                prev = new_history[i - 1]
                self.assertEqual(prev.get("role"), "assistant")
                self.assertTrue(msg_ends_with_tool_use(prev))


if __name__ == "__main__":
    unittest.main()
