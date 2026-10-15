"""Model ids come from an unauthenticated localhost API: what the collector keeps
for them must stay bounded however many different ids it is fed."""
import importlib.util
import itertools
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("llm_monitor", os.path.join(HERE, "..", "scripts", "llm-monitor.py"))
mon = importlib.util.module_from_spec(spec)
sys.modules["llm_monitor"] = mon
spec.loader.exec_module(mon)


class BoundedStateTest(unittest.TestCase):
    def setUp(self):
        mon._speed.clear()
        mon._last_output.clear()
        mon._context_memory.clear()

    def test_changing_huge_ids_do_not_grow_state(self):
        counter = itertools.count()

        def fake_http_json(_port, path):
            if path.startswith("/models"):
                n = next(counter)
                return {"data": [{"id": f"m{n}-" + "x" * 100000, "status": {"value": "loaded", "args": []}}]}
            if path.startswith("/slots"):    # generating, so the last reply is kept
                return [{"id_task": 1, "n_ctx": 4096, "n_prompt_tokens": 10, "is_processing": True,
                         "next_token": [{"n_decoded": 5}], "params": {"n_predict": 100}}]
            return None

        mon.http_json = fake_http_json
        mon.prometheus = lambda *_a, **_k: {}
        for _ in range(2000):
            mon.llamacpp_server_models([(1, "llama-server", "llama-server")])
        self.assertLessEqual(len(mon._speed), 1)          # only the model listed now
        self.assertLessEqual(len(mon._last_output), 1)
        self.assertTrue(all(len(key[1]) <= 120 for key in mon._speed))
        self.assertTrue(all(len(key) <= 120 for key in mon._last_output))

    def test_caches_capped(self):
        for n in range(1000):
            mon.speed_state(("vllm", n))
            mon.remember(mon._context_memory, ("bin", str(n)), n)
        self.assertEqual(len(mon._speed), mon.MAX_TRACKED)
        self.assertEqual(len(mon._context_memory), mon.MAX_TRACKED)
        self.assertIn(("vllm", 999), mon._speed)          # the newest stay

    def test_listed_model_keeps_its_state(self):
        mon.speed_state(("llamacpp", "a"))["rates"] = [10.0]
        mon.forget_models("llamacpp", {"a"})
        self.assertEqual(mon._speed[("llamacpp", "a")]["rates"], [10.0])
        mon.forget_models("llamacpp", {"b"})
        self.assertNotIn(("llamacpp", "a"), mon._speed)


if __name__ == "__main__":
    unittest.main()
