"""llama.cpp context figures: with a unified KV cache (the default when the number of
slots is automatic) every slot reports the whole pool as its n_ctx, so the slots must
not be added up; with one part per slot they must."""
import importlib.util
import json
import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("llm_monitor_ctx", os.path.join(HERE, "..", "scripts", "llm-monitor.py"))
mon = importlib.util.module_from_spec(spec)
sys.modules["llm_monitor_ctx"] = mon
spec.loader.exec_module(mon)
REAL_HTTP_JSON = mon.http_json


def serve(models=None, slots=None):
    def http_json(port, path, timeout=0.6):
        if path == "/models":
            return models
        return slots
    mon.http_json = http_json
    mon.context_memory = lambda pid: None


class ServerSettings(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(mon.server_settings([]), (None, True))

    def test_last_occurrence_wins(self):
        self.assertEqual(mon.server_settings(["-c", "4096", "--ctx-size", "8192"])[0], 8192)
        self.assertEqual(mon.server_settings(["--ctx-size", "8192", "-c", "4096"])[0], 4096)
        self.assertEqual(mon.server_settings(["-kvu", "--no-kv-unified"])[1], False)

    def test_slot_count_decides_the_cache_mode(self):
        self.assertFalse(mon.server_settings(["-np", "4"])[1])
        self.assertTrue(mon.server_settings(["-np", "-1"])[1])
        self.assertTrue(mon.server_settings(["-np", "4", "-kvu"])[1])

    def test_environment_fills_gaps(self):
        env = {"LLAMA_ARG_CTX_SIZE": "16384", "LLAMA_ARG_N_PARALLEL": "2"}
        self.assertEqual(mon.server_settings([], env), (16384, False))
        self.assertEqual(mon.server_settings(["-c", "4096"], {**env, "LLAMA_ARG_KV_UNIFIED": "1"}), (4096, True))

    def test_bad_values(self):
        for args in (["-c"], ["-c", "big"], ["-c", "0"], ["-c", "-5"]):
            self.assertIsNone(mon.server_settings(args)[0], args)


class Slots(unittest.TestCase):
    def test_unified_pool_counted_once(self):
        serve(slots=[{"id": i, "n_ctx": 8192, "n_prompt_tokens": 100} for i in range(4)])
        self.assertEqual(mon.llamacpp_slots("m", unified=True)["context"], 8192)

    def test_split_pool_adds_up(self):
        serve(slots=[{"id": i, "n_ctx": 2048, "n_prompt_tokens": 100} for i in range(4)])
        self.assertEqual(mon.llamacpp_slots("m", unified=False)["context"], 8192)

    def test_unlimited_replies_share_the_free_space(self):
        busy = {"n_ctx": 8192, "n_prompt_tokens": 1024, "is_processing": True,
                "params": {"n_predict": -1}, "next_token": [{"n_decoded": 100}]}
        serve(slots=[dict(busy, id=0, id_task=1), dict(busy, id=1, id_task=2)])
        result = mon.llamacpp_slots("m2", unified=True)
        self.assertEqual(result["output_used"], 200)
        self.assertEqual(result["output_limit"], 200 + 8192 - 2048)

    def test_malformed_slots(self):
        serve(slots=[{"n_ctx": "x", "n_prompt_tokens": [1], "params": 42, "next_token": "y", "is_processing": True}])
        result = mon.llamacpp_slots("m3")
        self.assertIsNone(result["context"])
        self.assertEqual(result["context_used"], 0)


class Router(unittest.TestCase):
    def test_context_from_slots_not_sum(self):
        serve({"data": [{"id": "m", "status": {"value": "loaded", "args": ["llama-server", "--ctx-size", "131072"]}}]},
              [{"id": i, "n_ctx": 131072, "n_prompt_tokens": 10 * i} for i in range(4)])
        model = mon.llamacpp_server_models([])[0]
        self.assertEqual(model["context"], 131072)
        self.assertEqual(model["context_used"], 60)

    def test_without_ctx_size(self):
        serve({"data": [{"id": "m", "status": {"value": "loaded", "args": ["llama-server"]}}]},
              [{"id": i, "n_ctx": 32768, "n_prompt_tokens": 0} for i in range(4)])
        self.assertEqual(mon.llamacpp_server_models([])[0]["context"], 32768)

    def test_args_of_the_wrong_type(self):
        serve({"data": [{"id": "m", "status": {"value": "loaded", "args": 42}}]}, [])
        self.assertIsNone(mon.llamacpp_server_models([])[0]["context"])


class NotANumber(unittest.TestCase):
    def setUp(self):
        self.http_get = mon.http_get

    def tearDown(self):
        mon.http_get = self.http_get

    def test_nan_reply_is_no_reply(self):
        mon.http_get = lambda *_a, **_k: b'{"models": [{"context_length": NaN}]}'
        self.assertIsNone(REAL_HTTP_JSON(11434, "/api/ps"))

    def test_nan_metric_is_skipped(self):
        mon.http_get = lambda *_a, **_k: b"vllm:kv_cache_usage_perc NaN\nvllm:num_requests_running 1\n"
        samples = mon.prometheus(8000)
        self.assertNotIn("vllm:kv_cache_usage_perc", samples)
        self.assertTrue(all(math.isfinite(v) for rows in samples.values() for _, v in rows))
        json.dumps(samples, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
