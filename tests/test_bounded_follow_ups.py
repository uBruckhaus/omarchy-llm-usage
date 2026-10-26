"""Whoever listens on the llama.cpp port decides how many models it lists: the
follow-up requests one snapshot makes must stay bounded in number and in time."""
import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("llm_monitor_follow_ups", os.path.join(HERE, "..", "scripts", "llm-monitor.py"))
mon = importlib.util.module_from_spec(spec)
sys.modules["llm_monitor_follow_ups"] = mon
spec.loader.exec_module(mon)


class BoundedFollowUpsTest(unittest.TestCase):
    def setUp(self):
        self.clock = [0.0]
        self.requests = []
        self.monotonic = mon.time.monotonic
        mon.time.monotonic = lambda: self.clock[0]

    def tearDown(self):
        mon.time.monotonic = self.monotonic  # the time module is shared by every test
        mon.prometheus = lambda *_a, **_k: {}

    def squatter(self, rows, delay):
        def fake_http_json(_port, path):
            self.requests.append(path)
            if path.startswith("/models"):
                return {"data": rows}
            self.clock[0] += delay   # every follow-up answers just inside the per-request deadline
            return []
        mon.http_json = fake_http_json

    def test_thousands_of_slow_models(self):
        rows = [{"id": f"m{n}", "status": {"value": "loaded", "args": []}} for n in range(5000)]
        self.squatter(rows, delay=mon.HTTP_DEADLINE - 0.01)
        models = mon.llamacpp_server_models([(1, "llama-server", "llama-server")])
        follow_ups = [path for path in self.requests if path.startswith("/slots")]
        self.assertLessEqual(len(models), mon.MAX_MODELS)
        self.assertLessEqual(len(follow_ups), mon.MAX_MODELS)
        self.assertLessEqual(self.clock[0], mon.FOLLOW_UP_BUDGET + mon.HTTP_DEADLINE)

    def test_loaded_models_after_many_unloaded_ones_are_found(self):
        rows = [{"id": f"idle{n}", "status": {"value": "unloaded"}} for n in range(100)]
        rows.append({"id": "real", "status": {"value": "loaded", "args": []}})
        self.squatter(rows, delay=0.0)
        models = mon.llamacpp_server_models([(1, "llama-server", "llama-server")])
        self.assertEqual([model["name"] for model in models], ["real"])

    def test_odd_shapes_give_no_models(self):
        for data in ([1, 2], {"data": "x"}, {"data": [1, "a", None, {"status": "loaded"}]}):
            mon.http_json = lambda _port, path, data=data: data if path.startswith("/models") else []
            self.assertEqual(mon.llamacpp_server_models([]), [])
        mon.http_json = lambda *_a: {"models": [{"key": "k", "loaded_instances": [{}] * 1000}] * 1000}
        mon.port_open = lambda _port: True
        self.assertLessEqual(len(mon.lmstudio_models([])), mon.MAX_MODELS)
        mon.http_json = lambda *_a: {"models": [{"name": "x"}] * 1000}
        self.assertLessEqual(len(mon.ollama_models()), mon.MAX_MODELS)


if __name__ == "__main__":
    unittest.main()
