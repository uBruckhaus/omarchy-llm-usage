"""free-gpu.sh with stubbed systemctl, notify-send, pgrep and lms on PATH (no real
service is ever queried or stopped): a unit is stopped only when it is active, and
a status query that fails (e.g. no user bus) is reported, never as "nothing running"."""
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "free-gpu.sh")

SYSTEMCTL = """#!/bin/sh
# systemctl --user is-active --quiet UNIT  |  systemctl --user stop UNIT
echo "$*" >> "$STUB_DIR/systemctl.log"
for last; do :; done
unit="${last%.service}"
case "$*" in
  *is-active*) eval "rc=\\${ACTIVE_$(echo "$unit" | tr - _):-3}"
               [ "$rc" = 1 ] && echo "Failed to connect to user scope bus" >&2
               exit "$rc" ;;
  *stop*) exit "${STOP_RC:-0}" ;;
esac
exit 1
"""


class FreeGpu(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        stubs = {
            "systemctl": SYSTEMCTL,
            "notify-send": '#!/bin/sh\nprintf "%s\\n" "$@" > "$STUB_DIR/notify"\n',
            "pgrep": "#!/bin/sh\nexit 1\n",
            "lms": '#!/bin/sh\necho lms >> "$STUB_DIR/lms.log"\nexit 1\n',
        }
        for name, body in stubs.items():
            path = os.path.join(self.dir, name)
            with open(path, "w") as handle:
                handle.write(body)
            os.chmod(path, 0o755)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_script(self, **env):
        full = {"PATH": f"{self.dir}:/usr/bin:/bin", "HOME": self.dir, "STUB_DIR": self.dir,
                **{key: str(value) for key, value in env.items()}}
        result = subprocess.run(["bash", SCRIPT], env=full, capture_output=True, text=True, timeout=30)
        with open(os.path.join(self.dir, "notify")) as handle:
            title, body = handle.read().splitlines()[-2:]
        with open(os.path.join(self.dir, "systemctl.log")) as handle:
            calls = handle.read().splitlines()
        return result.returncode, title, body, calls

    def test_nothing_running(self):
        code, title, _, calls = self.run_script(ACTIVE_llama_server=3, ACTIVE_ollama=4, ACTIVE_vllm=3)
        self.assertEqual(code, 0)
        self.assertEqual(title, "Nothing to free")
        self.assertFalse([call for call in calls if "stop" in call])

    def test_query_failure_is_not_nothing_running(self):
        code, title, body, calls = self.run_script(ACTIVE_llama_server=1, ACTIVE_ollama=1, ACTIVE_vllm=1)
        self.assertNotEqual(code, 0)
        self.assertNotEqual(title, "Nothing to free")
        self.assertIn("Could not check", body)
        for unit in ("llama-server", "ollama", "vllm"):
            self.assertIn(unit, body)
        self.assertFalse([call for call in calls if "stop" in call])

    def test_partial_query_failure(self):
        code, title, body, calls = self.run_script(ACTIVE_llama_server=0, ACTIVE_ollama=1, ACTIVE_vllm=4)
        self.assertNotEqual(code, 0)
        self.assertIn("Stopped: llama-server", body)
        self.assertIn("Could not check: ollama", body)
        self.assertEqual([call for call in calls if "stop" in call], ["--user stop llama-server.service"])

    def test_active_unit_is_stopped(self):
        code, title, body, _ = self.run_script(ACTIVE_llama_server=0, ACTIVE_ollama=3, ACTIVE_vllm=3)
        self.assertEqual(code, 0)
        self.assertEqual(title, "GPU memory freed")
        self.assertIn("llama-server", body)

    def test_failed_stop(self):
        code, title, body, _ = self.run_script(ACTIVE_llama_server=0, STOP_RC=1)
        self.assertNotEqual(code, 0)
        self.assertEqual(title, "GPU memory not fully freed")
        self.assertIn("Could not stop: llama-server", body)


if __name__ == "__main__":
    unittest.main()
