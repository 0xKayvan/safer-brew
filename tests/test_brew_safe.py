"""Offline integration tests: python3 -m unittest discover -s tests -v."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "brew-safe"
MOCK_CLI = r'''#!/usr/bin/env python3
import datetime
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import parse_qs, urlparse

root = Path(os.environ["BREW_SAFE_TEST_ROOT"])
config = json.loads((root / "config.json").read_text())
args = sys.argv[1:]
tool = Path(sys.argv[0]).name
now = datetime.datetime(2026, 10, 20, tzinfo=datetime.timezone.utc)

def record(event, **fields):
    entry = json.dumps({"event": event, "time": time.monotonic(), **fields}) + "\n"
    fd = os.open(root / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.write(fd, entry.encode())
    os.close(fd)

if tool == "date":
    if args == ["+%s"]:
        print(int(now.timestamp()))
    elif args[3] == "crash":
        # Exercise a worker that exits before it can write a result.
        print("invalid!")
    else:
        print(int(datetime.datetime.strptime(args[3], "%Y-%m-%dT%H:%M:%SZ")
                  .replace(tzinfo=datetime.timezone.utc).timestamp()))
elif tool == "brew":
    if args[0] == "outdated":
        print("\n".join(config.get("outdated", [])))
    elif args[0] == "info":
        pkg = args[-1]
        record("info", pkg=pkg, args=args)
        data = config["packages"].get(pkg, {})
        if data.get("unknown"):
            print("Error: No available formula or cask with the name " + pkg, file=sys.stderr)
            sys.exit(1)
        cask = data.get("kind", "cask" if "--cask" in args else "formula") == "cask"
        if (cask and "--formula" in args) or (not cask and "--cask" in args):
            print("Error: No available package of the requested type: " + pkg, file=sys.stderr)
            sys.exit(1)
        if data.get("info_error"):
            print(data["info_error"], file=sys.stderr)
            if data.get("partial_json"):
                print(json.dumps({"formulae": [{"tap": "homebrew/core",
                                                "ruby_source_path": "Formula/" + pkg + ".rb"}]}))
            sys.exit(1)
        if data.get("invalid_json"):
            print("not JSON")
            sys.exit(0)
        item = {"tap": data.get("tap", "homebrew/cask" if cask else "homebrew/core"),
                "ruby_source_path": ("Casks/" if cask else "Formula/") + pkg + ".rb"}
        if data.get("missing_source"):
            item["ruby_source_path"] = None
        item["installed"] = None if cask else []
        print(json.dumps({"casks" if cask else "formulae": [item]}))
    else:
        record("run", args=args)
elif tool in ("gh", "curl"):
    pkg = Path(parse_qs(urlparse(args[-1]).query)["path"][0]).stem
    data = config["packages"].get(pkg, {})
    record("start", pkg=pkg, tool=tool)
    time.sleep(data.get("delay", 0.05))
    record("end", pkg=pkg, tool=tool)
    if data.get("api_fail") or (tool == "gh" and data.get("fallback")):
        sys.exit(1)
    iso = "crash" if data.get("crash") else (
        now - datetime.timedelta(days=data.get("age", 10))
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(json.dumps([{"commit": {"committer": {"date": iso}},
                       "html_url": "https://example.test/commit/" + pkg}]))
'''


@unittest.skipUnless(shutil.which("jq"), "jq is required")
class BrewSafeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="brew-safe-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        cli = self.root / "bin"
        cli.mkdir()
        (self.root / "tmp").mkdir()
        for name in ("brew", "gh", "curl", "date"):
            path = cli / name
            path.write_text(MOCK_CLI)
            path.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": str(cli) + os.pathsep + os.environ["PATH"],
            "TMPDIR": str(self.root / "tmp"),
            "BREW_SAFE_TEST_ROOT": str(self.root),
        }

    def configure(self, packages, **extra):
        (self.root / "config.json").write_text(json.dumps({"packages": packages, **extra}))

    def run_safe(self, *args):
        result = subprocess.run(
            ["bash", str(SCRIPT), *args], env=self.env,
            text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(list((self.root / "tmp").iterdir()), [], result.stderr)
        return result

    def events(self, kind=None):
        path = self.root / "events.jsonl"
        entries = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [entry for entry in entries if kind is None or entry["event"] == kind]

    def max_active(self):
        active = peak = 0
        for event in self.events():
            active += (event["event"] == "start") - (event["event"] == "end")
            peak = max(peak, active)
        self.assertEqual(active, 0)
        return peak

    def test_parallel_limit_output_order_and_single_final_brew(self):
        packages = {f"pkg{i}": {"delay": 0.3 if i == 0 else 0.08} for i in range(6)}
        self.configure(packages)
        result = self.run_safe("-j", "2", "install", *packages)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.max_active(), 2)
        reports = [line.split()[0] for line in result.stdout.splitlines() if "✅" in line]
        self.assertEqual(reports, list(packages))
        self.assertEqual([e["args"] for e in self.events("run")], [["install", *packages]])
        self.assertGreater(self.events("run")[0]["time"], max(e["time"] for e in self.events("end")))

    def test_default_eight_workers(self):
        self.configure({f"pkg{i}": {"delay": 0.3} for i in range(10)})
        result = self.run_safe("install", *(f"pkg{i}" for i in range(10)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(self.max_active(), 1)
        self.assertLessEqual(self.max_active(), 8)

    def test_one_job_is_sequential_and_can_follow_subcommand(self):
        self.configure({"one": {}, "two": {}, "three": {}})
        result = self.run_safe("install", "--jobs", "1", "one", "two", "three")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.max_active(), 1)
        self.assertEqual(self.events("run")[0]["args"], ["install", "one", "two", "three"])

    def test_new_formula_and_cask_are_auto_detected_in_the_same_install(self):
        self.configure({"hello": {"kind": "formula"}, "firefox": {"kind": "cask"}})
        result = self.run_safe("install", "hello", "firefox")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([e["args"] for e in self.events("run")], [["install", "hello", "firefox"]])
        self.assertEqual(len(self.events("start")), 2)
        for event in self.events("info"):
            self.assertNotIn("--formula", event["args"])
            self.assertNotIn("--cask", event["args"])

    def test_explicit_formula_and_cask_types_are_respected(self):
        self.configure({"hello": {"kind": "formula"}, "firefox": {"kind": "cask"}})
        for flag, pkg in (("--formula", "hello"), ("--formulae", "hello"),
                          ("--cask", "firefox"), ("--casks", "firefox")):
            with self.subTest(flag=flag):
                result = self.run_safe("install", flag, pkg)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                expected_type = "--cask" if pkg == "firefox" else "--formula"
                self.assertIn(expected_type, self.events("info")[-1]["args"])
                self.assertEqual(self.events("run")[-1]["args"], ["install", flag, pkg])
        run_count = len(self.events("run"))
        result = self.run_safe("install", "--formula", "firefox")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(len(self.events("run")), run_count)

    def test_homebrew_lookup_errors_are_shown_and_partial_metadata_is_rejected(self):
        error = "Error: Could not fetch formula metadata: network unavailable"
        self.configure({"one": {"info_error": error, "partial_json": True}})
        result = self.run_safe("install", "one")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(error, result.stdout)
        self.assertNotIn("unknown to brew", result.stdout)
        self.assertEqual(self.events("start"), [])
        self.assertEqual(self.events("run"), [])

    def test_missing_or_invalid_source_metadata_is_reported_and_blocks_install(self):
        self.configure({"missing": {"missing_source": True}, "invalid": {"invalid_json": True}})
        result = self.run_safe("install", "missing", "invalid")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("source metadata", result.stdout)
        self.assertNotIn("unknown to brew", result.stdout)
        self.assertEqual(self.events("start"), [])
        self.assertEqual(self.events("run"), [])

    def test_mixed_results_fail_closed(self):
        packages = {"stable": {}, "fresh": {"age": 1}, "unknown": {"unknown": True},
                    "thirdparty": {"tap": "someone/tools"}, "offline": {"api_fail": True}}
        self.configure(packages)
        result = self.run_safe("install", *packages)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("https://example.test/commit/fresh", result.stdout)
        self.assertIn("No available formula or cask with the name unknown", result.stdout)
        self.assertIn("third-party tap", result.stdout)
        self.assertIn("GitHub API lookup failed", result.stdout)
        self.assertEqual(self.events("run"), [])

    def test_only_stable_retains_flags_and_order(self):
        self.configure({"older": {}, "fresh": {"age": 1}, "boundary": {"age": 5},
                        "offline": {"api_fail": True}})
        result = self.run_safe("--only-stable", "-j", "2", "reinstall", "--cask",
                               "older", "fresh", "boundary", "offline")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events("run")[0]["args"], ["reinstall", "--cask", "older", "boundary"])
        self.assertTrue(all("--cask" in e["args"] for e in self.events("info")))

    def test_only_stable_with_nothing_safe_does_not_run_brew(self):
        self.configure({"fresh": {"age": 0}})
        result = self.run_safe("--only-stable", "install", "fresh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events("run"), [])

    def test_force_still_runs_once_after_checks(self):
        self.configure({"stable": {}, "fresh": {"age": 0}})
        result = self.run_safe("--force", "install", "stable", "fresh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([e["args"] for e in self.events("run")], [["install", "stable", "fresh"]])

    def test_upgrade_expands_outdated_packages_and_curl_fallback(self):
        self.configure({"one": {"fallback": True}, "two": {}}, outdated=["one", "two"])
        result = self.run_safe("-j", "2", "upgrade")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.events("info")), 2)
        self.assertTrue(any(e["tool"] == "curl" for e in self.events("start")))
        self.assertEqual(self.events("run")[0]["args"], ["upgrade"])

    def test_worker_crash_is_unverified(self):
        self.configure({"crash": {"crash": True}, "stable": {}})
        result = self.run_safe("-j", "1", "install", "crash", "stable")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("package check failed unexpectedly", result.stdout)
        self.assertEqual(self.events("run"), [])

    def test_jobs_validation_and_missing_values(self):
        self.configure({})
        for value in ("0", "-1", "abc", "1.5", "65", "999999999999999999999", "08"):
            with self.subTest(value=value):
                result = self.run_safe("--jobs", value, "install", "one")
                self.assertEqual(result.returncode, 2)
                self.assertIn("jobs must be an integer", result.stderr)
        for flag in ("-j", "--jobs"):
            with self.subTest(flag=flag):
                self.assertEqual(self.run_safe("install", flag).returncode, 2)
        self.assertEqual(self.events(), [])

    def test_short_days_flag_after_subcommand_is_brews_debug_flag(self):
        self.configure({"fresh": {"age": 1}})
        result = self.run_safe("install", "-d", "fresh")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("within the last 5 days", result.stdout)
        result = self.run_safe("-d", "0", "install", "-d", "fresh")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_safe("install", "--days", "0", "fresh")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_safe("uninstall", "-d", "fresh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([e["args"] for e in self.events("run")],
                         [["install", "-d", "fresh"], ["install", "fresh"],
                          ["uninstall", "-d", "fresh"]])

    def test_passthrough_and_separator(self):
        self.configure({})
        result = self.run_safe("-j", "2", "tap", "homebrew/core", "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_safe("config", "--", "--jobs", "3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([e["args"] for e in self.events("run")],
                         [["tap", "homebrew/core", "--force"], ["config", "--jobs", "3"]])
        self.assertEqual(self.events("info"), [])

    def test_interrupt_removes_temporary_results_and_does_not_run_brew(self):
        self.configure({"one": {"delay": 0.5}, "two": {"delay": 0.5}})
        process = subprocess.Popen(["bash", str(SCRIPT), "install", "one", "two"],
                                   env=self.env, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not self.events("start") and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(self.events("start"), "checks never started")
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=5)
            self.assertEqual(process.returncode, 143)
            self.assertEqual(list((self.root / "tmp").iterdir()), [])
            self.assertEqual(self.events("run"), [])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


if __name__ == "__main__":
    unittest.main()
