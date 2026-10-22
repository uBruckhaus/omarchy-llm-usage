"""Whoever listens on a probed metrics port decides what the scrape body holds:
parsing it must stay linear in its size, below the HTTP body limit."""
import importlib.util
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("llm_monitor_metrics", os.path.join(HERE, "..", "scripts", "llm-monitor.py"))
mon = importlib.util.module_from_spec(spec)
sys.modules["llm_monitor_metrics"] = mon
spec.loader.exec_module(mon)


def scrape(body):
    mon.http_get = lambda *_a, **_k: body.encode()
    start = time.monotonic()
    samples = mon.prometheus(8000)
    return samples, time.monotonic() - start


class MetricsParsingTest(unittest.TestCase):
    def test_real_samples(self):
        samples, _ = scrape(
            '# HELP vllm:kv_cache_usage_perc KV-cache usage.\n'
            'vllm:kv_cache_usage_perc{engine="0",model_name="Qwen/Qwen3-8B"} 0.25\n'
            'vllm:cache_config_info{block_size="16",num_gpu_blocks="2048",note="a \\"q\\" b",} 1.0\n'
            'vllm:num_requests_running 2\n'
            'llamacpp:other 5\n')
        self.assertEqual(samples["vllm:kv_cache_usage_perc"], [({"engine": "0", "model_name": "Qwen/Qwen3-8B"}, 0.25)])
        self.assertEqual(samples["vllm:cache_config_info"][0][0]["num_gpu_blocks"], "2048")
        self.assertEqual(samples["vllm:num_requests_running"], [({}, 2.0)])
        self.assertNotIn("llamacpp:other", samples)

    def test_long_unmatched_label_word_is_linear(self):
        # One valid sample whose label set is a single long word with no '=' (the old
        # findall retried it from every position: quadratic), just under the body limit.
        word = "a" * (mon.HTTP_LIMIT - 64)
        samples, took = scrape("vllm:kv_cache_usage_perc{" + word + "} 0.5\n")
        self.assertEqual(samples, {})
        self.assertLess(took, 1)
        start = time.monotonic()
        self.assertIsNone(mon.metric_labels(word))
        self.assertIsNone(mon.metric_labels('x="1",' + word))
        self.assertLess(time.monotonic() - start, 1)

    def test_many_short_lines_capped(self):
        body = "".join('vllm:x{a="%d",' % n + "b" * 40 + "} 1\n" for n in range(20000))
        body += "".join('vllm:y{a="%d"} 1\n' % n for n in range(100000))
        samples, took = scrape(body)
        self.assertLessEqual(sum(len(v) for v in samples.values()), mon.MAX_METRIC_SAMPLES)
        self.assertLess(took, 2)

    def test_malformed_label_sets_dropped(self):
        for labels in ('a="1" b="2"', 'a=1', '="x"', 'a="1",,b="2"', 'a="unterminated'):
            self.assertIsNone(mon.metric_labels(labels), labels)
        self.assertEqual(mon.metric_labels(""), {})


if __name__ == "__main__":
    unittest.main()
