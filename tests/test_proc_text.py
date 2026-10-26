"""/proc text is chosen by whoever owns the process: another local user can give a
process a name with bytes that are not UTF-8, and it is read before the owner is
checked. Reading it must not raise, and a failing first snapshot must not end
the collector."""
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("llm_monitor_proc_text", os.path.join(HERE, "..", "scripts", "llm-monitor.py"))
mon = importlib.util.module_from_spec(spec)
sys.modules["llm_monitor_proc_text"] = mon
spec.loader.exec_module(mon)


class ProcText(unittest.TestCase):
    def test_invalid_utf8_is_read(self):
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(b"llama\xff\xfe-server\n")
        try:
            text = mon.read(handle.name)
        finally:
            os.unlink(handle.name)
        self.assertIsInstance(text, str)
        self.assertTrue(text.startswith("llama"))
        json.dumps(mon.clean(text), allow_nan=False)

    def test_failing_first_snapshot_keeps_the_collector(self):
        calls = []

        def snapshot():
            calls.append(1)
            if len(calls) == 1:
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
            return {"ok": True}
        out = io.StringIO()
        with mock.patch.object(mon, "snapshot", snapshot), mock.patch.object(mon.time, "sleep", lambda _s: None), \
                mock.patch.object(sys, "argv", ["llm-monitor.py", "--once"]), mock.patch.object(sys, "stdout", out), \
                mock.patch.object(mon, "install_exit_handlers", lambda: None):
            self.assertEqual(mon.main(), 0)
        self.assertEqual(json.loads(out.getvalue().splitlines()[-1]), {"ok": True})


if __name__ == "__main__":
    unittest.main()
