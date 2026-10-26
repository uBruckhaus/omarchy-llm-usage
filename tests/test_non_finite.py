"""Valid JSON such as 1e999 parses to infinity, and finite metric samples can still
add up to infinity: neither may reach the snapshot, which the bar reads with
JSON.parse (no NaN, no Infinity)."""
import http.server
import importlib.util
import json
import math
import os
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("llm_monitor_finite", os.path.join(HERE, "..", "scripts", "llm-monitor.py"))
mon = importlib.util.module_from_spec(spec)
sys.modules["llm_monitor_finite"] = mon
spec.loader.exec_module(mon)
REAL = {name: getattr(mon, name) for name in ("http_json", "http_get", "port_open", "prometheus")}


def strict(text):
    """Parse like JSON.parse: NaN, Infinity and -Infinity are errors."""
    def refuse(name):
        raise ValueError(name)
    return json.loads(text, parse_constant=refuse)


def no_non_finite(value):
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(no_non_finite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return all(no_non_finite(item) for item in value)
    return True


class Reply(http.server.BaseHTTPRequestHandler):
    body = b"{}"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *_args):
        pass


class LocalhostReply(unittest.TestCase):
    """A real HTTP reply on 127.0.0.1 with an overflowing number."""

    def setUp(self):
        self.server = http.server.HTTPServer(("127.0.0.1", 0), Reply)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_1e999_reply_is_no_reply(self):
        Reply.body = b'{"models": [{"name": "m", "context_length": 1e999, "size_vram": -1e999}]}'
        self.assertIsNone(REAL["http_json"](self.port, "/api/ps"))

    def test_finite_reply_still_parses(self):
        Reply.body = b'{"models": [{"name": "m", "context_length": 4096.0}]}'
        self.assertEqual(REAL["http_json"](self.port, "/api/ps")["models"][0]["context_length"], 4096.0)


class RuntimeFields(unittest.TestCase):
    """Numbers that got past parsing are checked per field (type, finite, range)."""

    def setUp(self):
        mon.port_open = lambda _port: True
        mon.prometheus = lambda *_a, **_k: {}

    def tearDown(self):
        for name, value in REAL.items():
            setattr(mon, name, value)

    BAD = (float("inf"), float("-inf"), float("nan"), 10**30, -5, "4096", "<b>x</b>", [1], {"a": 1}, True)

    def test_ollama(self):
        for bad in self.BAD:
            mon.http_json = lambda *_a, bad=bad: {"models": [{"name": "m", "context_length": bad, "size_vram": bad}]}
            model = mon.ollama_models()[0]
            self.assertIsNone(model["context"], bad)
            self.assertIsNone(model["vram"], bad)
            strict(json.dumps(model))
        mon.http_json = lambda *_a: {"models": [{"name": "m", "context_length": 8192, "size_vram": 2**30}]}
        model = mon.ollama_models()[0]
        self.assertEqual((model["context"], model["vram"]), (8192, 2**30))

    def test_lmstudio(self):
        for bad in self.BAD:
            mon.http_json = lambda *_a, bad=bad: {"models": [{"key": "k", "loaded_instances": [{"config": {"context_length": bad}}]}]}
            model = mon.lmstudio_models([])[0]
            self.assertIsNone(model["context"], bad)
            strict(json.dumps(model))

    def test_vllm(self):
        for bad in self.BAD:
            mon.http_json = lambda *_a, bad=bad: {"data": [{"id": "m", "max_model_len": bad}]}
            model = mon.vllm_models([])[0]
            self.assertIsNone(model["context"], bad)
            strict(json.dumps(model))

    def test_llamacpp_slots(self):
        mon.http_json = lambda _port, path: [{"n_ctx": 1e300, "n_prompt_tokens": 10**40, "is_processing": True,
                                              "params": {"n_predict": 1e300}, "next_token": [{"n_decoded": 1e300}]}]
        slots = mon.llamacpp_slots("m")
        self.assertTrue(no_non_finite(slots))
        strict(json.dumps(slots))
        for key in ("context", "context_used", "output_used", "output_limit"):
            self.assertLessEqual(slots[key] or 0, mon.MAX_QUANTITY, key)


class MetricSums(unittest.TestCase):
    """Each sample is finite, but their sums and quotients overflow."""

    def tearDown(self):
        for name, value in REAL.items():
            setattr(mon, name, value)
        mon._speed.clear()

    def test_huge_samples_are_skipped(self):
        mon.http_get = lambda *_a, **_k: b'vllm:generation_tokens_total{a="1"} 1e308\nvllm:generation_tokens_total{a="2"} 1e308\n'
        samples = mon.prometheus(8000)
        total = sum(value for _, value in samples.get("vllm:generation_tokens_total", []))
        self.assertTrue(math.isfinite(total))

    def test_vllm_speed_stays_finite(self):
        lines = [b'vllm:num_requests_running 1e300', b'vllm:generation_tokens_total 1e300',
                 b'vllm:cache_config_info{num_gpu_blocks="99999999999999999999",block_size="99999999999999999999"} 1',
                 b'vllm:kv_cache_usage_perc 1']
        mon.http_get = lambda *_a, **_k: b"\n".join(lines) + b"\n"
        mon.http_json = lambda *_a: {"data": [{"id": "m", "max_model_len": 4096}]}
        for _ in range(3):
            models = mon.vllm_models([])
        self.assertTrue(no_non_finite(models))
        strict(json.dumps(models))

    def test_llamacpp_speed_stays_finite(self):
        state = mon.speed_state(("llamacpp", "m"))
        mon.prometheus = lambda *_a, **_k: {"llamacpp:tokens_predicted_total": [({}, 1e15)],
                                            "llamacpp:tokens_predicted_seconds_total": [({}, 5e-324)]}
        mon.llamacpp_speed("m", state)
        mon.prometheus = lambda *_a, **_k: {"llamacpp:tokens_predicted_total": [({}, 2e15)],
                                            "llamacpp:tokens_predicted_seconds_total": [({}, 1e-323)]}
        mon.llamacpp_speed("m", state)
        report = mon.speed_report(state)
        self.assertTrue(no_non_finite(report))
        strict(json.dumps(report))


class SnapshotWriter(unittest.TestCase):
    """The last line of defence: whatever slipped through, the line stays valid JSON."""

    def test_non_finite_becomes_null(self):
        line = mon.encode({"a": float("inf"), "b": [1.5, float("nan")], "c": {"d": -float("inf")}, "e": "x"})
        self.assertEqual(strict(line), {"a": None, "b": [1.5, None], "c": {"d": None}, "e": "x"})

    def test_unserialisable_snapshot_gives_an_error_line(self):
        data = strict(mon.encode({"a": object()}))
        self.assertIn("error", data)


if __name__ == "__main__":
    unittest.main()
