##
 # @file test/test_vector_memory_issue68.py
 # @date 2026/08/19
 # @brief Unit tests for Issue #68 vector memory, UTF-8/ASCII fallback, and memory_search.
 #

import json, os, tempfile, unittest
from src.core.utils.config import load_api_config
from src.core.utils.embedding import (
    EmbeddingError, GeminiEmbeddingProvider, MockEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider, create_embedding_provider, validate_and_normalize_vector,
)
from src.core.agent.memory import MemoryManager
from src.core.agent.sysprompt import BasePromptBuilder
from src.core.tool.agent.memory_tool import MemoryTool, ForgetMemoryTool, MemorySearchTool
from src.core.subagent.registry import TOOLSET_REGISTRY, resolve_toolset
from src.remote.execution.agent import MAIN_DIRECT_TOOLS


##
 # @brief Test suite covering config, providers, write-time guard, fallback, and tools.
 #
class TestVectorMemoryIssue68(unittest.TestCase):
    ##
     # @brief Test [Memory] and [Embedding] config parsing and invalid provider fallback.
     #
    def test_config_parsing_and_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = os.path.join(td, "api.cfg")
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write(
                    '[Main]\nMAIN_AGENT=m1\n[P]\nSDK_TYPE="Message"\nAPI_KEY=k\n'
                    'MODEL_LIST=[{"provider":{"model_id":"id"},"basic":{"name":"m1"}}]\n'
                    '[Memory]\nPASSIVE_TOP_K=3\nMIN_SIMILARITY=0.4\n'
                    '[Embedding]\nPROVIDER=mock\nMODEL=mock-semantic-v1\nDIMENSIONS=8\n'
                )
            # End-with
            cfg = load_api_config(cfg_path)
            self.assertEqual(cfg["MEMORY"]["passive_top_k"], 3)
            self.assertAlmostEqual(cfg["MEMORY"]["min_similarity"], 0.4)
            self.assertEqual(cfg["MEMORY"]["provider"], "mock")

            # Missing API_KEY for gemini falls back to disabled without breaking chat config
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write(
                    '[Main]\nMAIN_AGENT=m1\n[P]\nSDK_TYPE="Message"\nAPI_KEY=k\n'
                    'MODEL_LIST=[{"provider":{"model_id":"id"},"basic":{"name":"m1"}}]\n'
                    '[Embedding]\nPROVIDER=gemini\nMODEL=gemini-embedding-001\n'
                )
            # End-with
            cfg2 = load_api_config(cfg_path)
            self.assertEqual(cfg2["MEMORY"]["provider"], "disabled")
        # End-with
    # End-def

    ##
     # @brief Test Gemini and OpenAI-compatible providers with fake HTTP transport.
     #
    def test_embedding_providers(self):
        calls = []
        def fake_gemini(method, url, headers, payload, timeout_s):
            calls.append((url, headers, payload))
            if ":embedContent" in url:
                return 200, {"embedding": {"values": [3.0, 4.0, 0.0, 0.0]}}
            # End-if
            return 200, {"embeddings": [{"values": [0.0, 3.0, 4.0, 0.0]} for _ in payload["requests"]]}
        # End-def

        gem = GeminiEmbeddingProvider("gemini-embedding-001", "g-key", dimensions=4, batch_size=2, transport=fake_gemini)
        qv = gem.embed_query("hello")
        self.assertEqual(len(qv), 4)
        self.assertAlmostEqual(qv[0], 0.6)
        self.assertEqual(calls[0][2]["taskType"], "RETRIEVAL_QUERY")
        dvs = gem.embed_documents(["a", "b", "c"])
        self.assertEqual(len(dvs), 3)
        self.assertEqual(calls[1][2]["requests"][0]["taskType"], "RETRIEVAL_DOCUMENT")

        def fake_oai(method, url, headers, payload, timeout_s):
            return 200, {"data": [{"index": 1, "embedding": [0.0, 1.0]}, {"index": 0, "embedding": [1.0, 0.0]}]}
        # End-def
        oai = OpenAICompatibleEmbeddingProvider("text-embedding-3-small", "o-key", "https://api.example.com/v1", dimensions=2, transport=fake_oai)
        res = oai.embed_documents(["first", "second"])
        self.assertEqual(res[0], [1.0, 0.0])
        self.assertEqual(res[1], [0.0, 1.0])
    # End-def

    ##
     # @brief Test write-time UTF-8 acceptance when vector enabled vs ASCII-retry error when disabled/failing.
     #
    def test_write_time_utf8_guard_and_cjk_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            g_dir = os.path.join(td, "global")
            s_dir = os.path.join(td, "session")
            # 1. Vector disabled: non-ASCII content is rejected asking LLM to translate to English/ASCII
            mm_disabled = MemoryManager(memory_dir=g_dir, session_memory_dir=s_dir, memory_config={"provider": "disabled"})
            tool_disabled = MemoryTool(mm_disabled)
            ok, msg = tool_disabled.execute(name="food_pref", description="\u5ddd\u83dc\u504f\u597d", content="\u7528\u6237\u559c\u6b22\u624b\u6495\u5154")
            self.assertFalse(ok)
            self.assertIn("English/ASCII", msg)
            ok_ascii, _ = tool_disabled.execute(name="food_pref", description="Sichuan food", content="User likes spicy rabbit")
            self.assertTrue(ok_ascii)

            # 2. Vector enabled (mock): UTF-8 content succeeds and is cached in SQLite
            mock_prov = MockEmbeddingProvider()
            mm_vec = MemoryManager(memory_dir=g_dir, session_memory_dir=s_dir, embedding_provider=mock_prov)
            tool_vec = MemoryTool(mm_vec)
            ok_utf8, _ = tool_vec.execute(name="gym_note", description="\u5065\u8eab\u8bb0\u5f55", tags="\u5367\u63a8, \u529b\u91cf", content="\u5468\u4e09\u5367\u63a8 90kg 5x5")
            self.assertTrue(ok_utf8)
            self.assertTrue(os.path.exists(os.path.join(g_dir, ".vector_cache.sqlite3")))

            # Non-ASCII name is still rejected even when vector is enabled
            ok_bad_name, _ = tool_vec.execute(name="\u4e2d\u6587\u540d", description="desc", content="body")
            self.assertFalse(ok_bad_name)

            # Cross-lingual vector search: English query matches Chinese memory
            sr = mm_vec.search("What is my gym bench workout?", scope="all", top_k=3)
            self.assertEqual(sr["backend"], "vector")
            self.assertEqual(sr["hits"][0]["name"], "gym_note")

            # 3. Simulate embedding failure -> write rejects non-ASCII, search falls back to CJK 2-gram split
            mock_prov.fail_mode = "http_500"
            ok_fail, msg_fail = tool_vec.execute(name="other_cn", description="\u6d4b\u8bd5", content="\u5185\u5bb9")
            self.assertFalse(ok_fail)
            self.assertIn("English/ASCII", msg_fail)

            # Previously saved UTF-8 memory is still retrievable via CJK 2-gram fallback during cooldown!
            fb = mm_vec.search("\u67e5\u8be2\u6211\u7684\u5065\u8eab\u5367\u63a8\u8ba1\u5212", scope="all", top_k=3)
            self.assertEqual(fb["backend"], "keyword")
            self.assertEqual(fb["hits"][0]["name"], "gym_note")
        # End-with
    # End-def

    ##
     # @brief Test session-before-global tie-break, passive interrupt filtering, MemorySearchTool, and wiring.
     #
    def test_search_tool_tiebreak_and_wiring(self):
        with tempfile.TemporaryDirectory() as td:
            mm = MemoryManager(
                memory_dir=os.path.join(td, "g"),
                session_memory_dir=os.path.join(td, "s"),
                embedding_provider=MockEmbeddingProvider(),
            )
            mm.write_memory("ota_rule", "OTA partition table", "ota, sdio", "Global OTA note", scope="global")
            mm.write_memory("ota_rule", "OTA partition table", "ota, sdio", "Session OTA note", scope="session")
            res = mm.search("ota sdio partition", scope="all", top_k=5)
            self.assertEqual(res["hits"][0]["scope"], "session")

            # Passive retrieval skips non-user Remote interrupt envelopes and query_override=None
            self.assertEqual(mm.load_memories_string([{"role": "user", "content": "[Interrupt: Scheduled Wakeup | id=1]\ncheck"}]), "")
            self.assertEqual(mm.load_memories_string([{"role": "user", "content": "ota sdio"}], query_override=None), "")
            self.assertIn("Session OTA note", mm.load_memories_string([{"role": "user", "content": "ota sdio"}]))

            # MemorySearchTool execution and forget cleanup
            ms = MemorySearchTool(mm)
            ok, payload = ms.execute(query="ota firmware", scope="global", top_k=2)
            self.assertTrue(ok)
            parsed = json.loads(payload)
            self.assertEqual(parsed["hits"][0]["scope"], "global")
            self.assertTrue(ForgetMemoryTool(mm).execute(name="ota_rule", scope="global")[0])

            # Toolset reachability across SubAgent & Remote
            self.assertIn("memory_search", TOOLSET_REGISTRY["code_analysis"])
            self.assertIn("memory_search", TOOLSET_REGISTRY["full"])
            self.assertIn("memory_search", MAIN_DIRECT_TOOLS)
            resolved = resolve_toolset("code_analysis", {"memory_search": ms})
            self.assertIn("memory_search", resolved)

            # Language policy in sysprompt aligns with UTF-8 primary + ASCII fallback
            lang_sec = BasePromptBuilder._build_language_section()
            self.assertIn("'name' and 'scope' MUST be English/ASCII", lang_sec)
            self.assertIn("UTF-8", lang_sec)
        # End-with
    # End-def
# End-class


if __name__ == "__main__":
    unittest.main()
