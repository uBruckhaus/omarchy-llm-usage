"""llama-fit-params is started without Python code between fork and exec
(no preexec_fn) and ends with the collector: by util-linux setpriv's parent-death
signal when the collector is killed, and by the collector itself when it is
told to stop while it waits."""
import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "llm-monitor.py")
spec = importlib.util.spec_from_file_location("llm_monitor_helper", SCRIPT)
mon = importlib.util.module_from_spec(spec)
sys.modules["llm_monitor_helper"] = mon
spec.loader.exec_module(mon)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie no one reaps yet counts as gone.
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def wait_gone(pid, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


class HelperArgv(unittest.TestCase):
    def setUp(self):
        self.which = mon.shutil.which

    def tearDown(self):
        mon.shutil.which = self.which

    def test_setpriv_prefix(self):
        mon.shutil.which = lambda name: "/usr/bin/setpriv" if name == "setpriv" else None
        argv = mon.helper_argv(["/x/llama-fit-params", "-m", "a"])
        self.assertEqual(argv[:4], ["/usr/bin/setpriv", "--pdeathsig", "KILL", "--"])
        self.assertEqual(argv[-4:], [str(os.getpid()), "/x/llama-fit-params", "-m", "a"])

    @unittest.skipUnless(shutil.which("setpriv"), "util-linux setpriv not installed")
    def test_guard_stops_an_orphaned_helper(self):
        """A collector that died before the parent-death signal was set: the
        helper finds another parent and does not run."""
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "ran")
            result = subprocess.run(mon.helper_argv(["touch", marker], parent=1), timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(os.path.exists(marker))
            result = subprocess.run(mon.helper_argv(["touch", marker]), timeout=10)
            self.assertEqual(result.returncode, 0)
            self.assertTrue(os.path.exists(marker))

    def test_without_setpriv_nothing_runs(self):
        """Without a parent-death signal a collector killed with SIGKILL (GNOME's
        force_exit) would leave the helper behind: the projection is skipped."""
        mon.shutil.which = lambda name: None
        self.assertIsNone(mon.helper_argv(["/x/llama-fit-params"]))
        real = mon.subprocess.Popen

        def refuse(*_a, **_k):
            raise AssertionError("a helper was started without setpriv")
        mon.subprocess.Popen = refuse
        try:
            self.assertIsNone(mon.run_helper(["/x/llama-fit-params"], timeout=5))
        finally:
            mon.subprocess.Popen = real

    @unittest.skipUnless(shutil.which("setpriv"), "util-linux setpriv not installed")
    def test_output_is_bounded(self):
        start = time.monotonic()
        self.assertIsNone(mon.run_helper(["sh", "-c", "exec yes"], timeout=20, limit=2**20))
        self.assertIsNone(mon.run_helper(["sh", "-c", "head -c 3000000 /dev/zero >&2"], timeout=20, limit=2**20))
        self.assertLess(time.monotonic() - start, 10)

    @unittest.skipUnless(shutil.which("setpriv"), "util-linux setpriv not installed")
    def test_timeout_kills_the_helper(self):
        start = time.monotonic()
        self.assertIsNone(mon.run_helper(["sleep", "30"], timeout=0.5))
        self.assertLess(time.monotonic() - start, 5)

    @unittest.skipUnless(shutil.which("setpriv"), "util-linux setpriv not installed")
    def test_output_of_both_streams(self):
        text = mon.run_helper(["sh", "-c", "echo one >&2; echo two; printf '\\377'"], timeout=10)
        self.assertIn("one", text)
        self.assertIn("two", text)

    def test_no_preexec_fn(self):
        seen = {}
        real = mon.subprocess.Popen

        def fake_popen(argv, **kwargs):
            seen.update(kwargs, argv=argv)
            return real(["true"], **kwargs)
        mon.subprocess.Popen = fake_popen
        try:
            mon.run_helper(["/nonexistent/llama-fit-params"], timeout=5)
        finally:
            mon.subprocess.Popen = real
        self.assertIsNone(seen.get("preexec_fn"))
        self.assertNotIn("_die_with_parent", dir(mon))


class HelperEndsWithCollector(unittest.TestCase):
    """A stand-in collector runs a helper that would sleep for a minute."""

    def start(self):
        self.tmp = tempfile.mkdtemp()
        helper = os.path.join(self.tmp, "helper.sh")
        with open(helper, "w") as handle:
            handle.write(f'#!/bin/sh\necho $$ > "{self.tmp}/pid"\nexec sleep 60\n')
        os.chmod(helper, 0o755)
        code = ("import importlib.util, sys\n"
                f"spec = importlib.util.spec_from_file_location('m', {SCRIPT!r})\n"
                "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
                "m.install_exit_handlers()\n"
                f"m.run_helper([{helper!r}], timeout=60)\n")
        collector = subprocess.Popen([sys.executable, "-c", code])
        pid_file = os.path.join(self.tmp, "pid")
        end = time.monotonic() + 10
        while time.monotonic() < end and collector.poll() is None:
            if os.path.exists(pid_file) and os.path.getsize(pid_file):
                break
            time.sleep(0.05)
        if collector.poll() is not None:
            self.fail("the stand-in collector ended before its helper started")
        time.sleep(0.2)  # the helper has exec'd into sleep
        with open(pid_file) as handle:
            return collector, int(handle.read())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @unittest.skipUnless(shutil.which("setpriv"), "util-linux setpriv not installed")
    def test_sigterm_ends_the_helper(self):
        collector, helper = self.start()
        collector.send_signal(signal.SIGTERM)
        collector.wait(10)
        self.assertTrue(wait_gone(helper), "helper still running after SIGTERM to the collector")

    @unittest.skipUnless(shutil.which("setpriv"), "util-linux setpriv not installed")
    def test_sigkill_ends_the_helper(self):
        collector, helper = self.start()
        collector.kill()
        collector.wait(10)
        self.assertTrue(wait_gone(helper), "helper still running after the collector was killed")


if __name__ == "__main__":
    unittest.main()
