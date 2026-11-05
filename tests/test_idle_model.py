"""llama.cpp's --sleep-idle-seconds frees the card only when no request comes in.
The collector asks a loaded model's /slots and /metrics only while its process
works (and once right after), so an idle model can go to sleep."""
import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("llm_monitor_idle", os.path.join(HERE, "..", "scripts", "llm-monitor.py"))
mon = importlib.util.module_from_spec(spec)
sys.modules["llm_monitor_idle"] = mon
spec.loader.exec_module(mon)


class IdleModelTest(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.ticks = [1000]
        self.written = [0]
        self.saved = (mon.http_json, mon.prometheus, mon.server_for, mon.cpu_ticks, mon.context_memory, mon.written, mon.read)
        mon._quiet.clear()
        mon._ticks.clear()
        mon._looked.clear()
        rows = [{"id": "gemma", "status": {"value": "loaded", "args": ["llama-server", "-m", "/m/gemma.gguf"]}}]

        def fake_http_json(_port, path):
            self.requests.append(path)
            if path.startswith("/models"):
                return {"data": rows}
            self.written[0] += 300   # answering us: the model writes to its socket
            self.ticks[0] += 1
            return [{"n_ctx": 4096, "n_prompt_tokens": 100, "is_processing": False}]
        mon.http_json = fake_http_json
        mon.prometheus = lambda *_a, **_k: {}
        mon.server_for = lambda _processes, _args: 4242
        mon.cpu_ticks = lambda _pid: self.ticks[0]
        mon.context_memory = lambda _pid: None
        mon.written = lambda _pid: self.written[0]

    def tearDown(self):
        (mon.http_json, mon.prometheus, mon.server_for, mon.cpu_ticks, mon.context_memory, mon.written, mon.read) = self.saved

    def look(self):
        """One snapshot; the model's own requests it made."""
        before = len(self.requests)
        models = mon.llamacpp_server_models([(4242, "llama-server", "llama-server")])
        return models, [p for p in self.requests[before:] if not p.startswith("/models")]

    def test_an_idle_model_is_left_alone_and_keeps_its_figures(self):
        models, asked = self.look()            # first look after the load
        self.assertTrue(any(p.startswith("/slots") for p in asked))
        self.assertEqual(models[0]["context"], 4096)
        self.ticks[0] += 1                     # idle: no work
        models, asked = self.look()            # once more right after (was "busy" when first seen)
        for _ in range(5):
            self.ticks[0] += 1
            models, asked = self.look()
            self.assertEqual(asked, [])        # nothing that would keep it awake
        self.assertEqual(models[0]["context"], 4096)
        self.assertEqual(models[0]["context_used"], 100)
        self.assertFalse(models[0]["generating"])

    def test_a_working_model_is_asked_and_once_more_after_it_stops(self):
        self.look()
        self.ticks[0] += 1
        self.look()
        self.ticks[0] += 1
        self.assertEqual(self.look()[1], [])
        self.ticks[0] += 200                   # generating
        self.assertTrue(self.look()[1])
        self.ticks[0] += 1                     # just stopped: the final counters
        self.assertTrue(self.look()[1])
        self.ticks[0] += 1
        self.assertEqual(self.look()[1], [])

    def test_its_answer_to_us_does_not_count_as_work(self):
        # The router's own /models aside, an idle model gets no request at all,
        # although answering our first two looks made it write and compute.
        self.look()
        self.look()
        for _ in range(5):
            self.assertEqual(self.look()[1], [])

    def test_a_reply_with_little_cpu_is_seen_by_what_it_writes(self):
        self.look()
        self.look()
        self.assertEqual(self.look()[1], [])
        self.written[0] += 2000                # a reply streams out, CPU barely moves
        self.ticks[0] += 1
        self.assertTrue(self.look()[1])

    def test_a_single_model_server_is_not_asked_for_models_when_idle(self):
        asked, models = self.single_server(["llama-server", "-m", "/m/gemma.gguf", "--port", "8080"])
        self.assertEqual(asked, [])   # not even /models
        self.assertEqual(models[0]["name"], "gemma")
        self.assertEqual(models[0]["context"], 4096)

    def single_server(self, argv):
        """One llama-server with its own model: (requests while idle, its models)."""
        real_read = self.saved[-1]
        mon.read = lambda path, default="": ("\0".join(argv) if path.endswith("/cmdline")
                                              else real_read(path, default))
        processes = [(4242, "llama-server", " ".join(argv))]
        for _ in range(2):
            mon.llamacpp_server_models(processes)
        before = len(self.requests)
        for _ in range(5):
            models = mon.llamacpp_server_models(processes)
        return self.requests[before:], models

    def test_a_hugging_face_single_model_server_is_not_asked_for_models(self):
        asked, models = self.single_server(["llama-server", "-hf", "unsloth/gemma-3-GGUF:Q4_K_M"])
        self.assertEqual(asked, [])
        self.assertEqual(models[0]["name"], "gemma-3-GGUF")

    def test_a_model_path_with_spaces_keeps_its_name(self):
        asked, models = self.single_server(["llama-server", "-m", "/home/u/My Models/gemma 4.gguf"])
        self.assertEqual(asked, [])
        self.assertEqual(models[0]["name"], mon.clean("gemma 4"))

    def test_a_model_url_shows_no_query(self):
        asked, models = self.single_server(
            ["llama-server", "-mu", "https://host/models/gemma.gguf?token=secret#part"])
        self.assertEqual(asked, [])
        self.assertEqual(models[0]["name"], "gemma")
        self.assertEqual(mon.model_of_argv(["llama-server", "--model-url", "https://h/?x=secret"]), "")

    def test_a_router_is_still_asked_for_its_models(self):
        real_read = self.saved[-1]
        mon.read = lambda path, default="": ("llama-server\0--models-preset\0/x/models.ini"
                                              if path.endswith("/cmdline") else real_read(path, default))
        mon.llamacpp_server_models([(4242, "llama-server", "llama-server --models-preset /x/models.ini")])
        self.assertIn("/models", self.requests)

    def test_an_unreadable_io_file_is_asked_every_time(self):
        mon.written = lambda _pid: None        # another user's service: io not readable
        for _ in range(4):
            self.ticks[0] += 1
            self.assertTrue(self.look()[1])    # unknown: no reply is missed

    def test_an_unknown_process_is_asked_as_before(self):
        mon.cpu_ticks = lambda _pid: None
        self.look()
        self.assertTrue(self.look()[1])

    def test_a_model_no_longer_loaded_is_forgotten(self):
        self.look()
        self.assertIn("gemma", mon._quiet)
        mon.http_json = lambda _port, path: {"data": []} if path.startswith("/models") else []
        mon.llamacpp_server_models([])
        self.assertNotIn("gemma", mon._quiet)


if __name__ == "__main__":
    unittest.main()
