#!/usr/bin/env python3
"""Stdio integration tests for prompts-dir-mcp (2026-07-28 prompts +
completion + subscriptions listChanged).

Spawns the server per test with an isolated template directory and talks
JSON-RPC over pipes. Reader thread + queue, Windows-safe.
"""

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "prompts_dir_server.py")
EXAMPLES = os.path.join(HERE, "prompts")
SUB_ID = "io.modelcontextprotocol/subscriptionId"


class Session:
    def __init__(self, root):
        env = dict(os.environ)
        env["PROMPTS_DIR"] = root
        env["PROMPTS_MCP_POLL_MS"] = "30"
        self.proc = subprocess.Popen(
            [sys.executable, SERVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, cwd=root,
        )
        self.q = queue.Queue()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self.q.put(json.loads(line))
            except ValueError:
                self.q.put({"_unparsable": line})
        self.q.put(None)

    def send(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def recv(self, timeout=5.0):
        return self.q.get(timeout=timeout)

    def initialize(self):
        self.send({"jsonrpc": "2.0", "id": "init", "method": "initialize",
                   "params": {"protocolVersion": "2026-07-28"}})
        return self.recv()

    def drain(self, seconds):
        out = []
        deadline = time.time() + seconds
        while True:
            left = deadline - time.time()
            if left <= 0:
                break
            try:
                m = self.q.get(timeout=left)
            except queue.Empty:
                break
            if m is None:
                break
            out.append(m)
        return out

    def close(self, expect_exit=True):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        err = self.proc.stderr.read()
        if expect_exit and (self.proc.returncode != 0 or err):
            raise AssertionError("server exit=%s stderr=%r" % (self.proc.returncode, err))


REVIEW = """<!-- args
code: The code to review (required)
language: Programming language [python,javascript,go,rust]
-->
# Request Code Review
Asks the LLM to analyze code quality.

Please review this {{language}} code:

{{code}}
"""

SUMMARIZE = """<!-- args
text: The text to summarize (required)
style: Output style [brief,detailed,bullets]
-->
Summarize text into a chosen style.

Summarize the following in {{style}} style:

{{text}}
"""

PLAIN = """A description line here.

Just a fixed body with no placeholders.
"""


class PromptsDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.session = Session(self.root)

    def tearDown(self):
        self.session.close(expect_exit=False)
        self.tmp.cleanup()

    def write(self, name, content):
        with open(os.path.join(self.root, name), "w", newline="") as f:
            f.write(content)

    def settle(self, seconds=0.3):
        time.sleep(seconds)

    # -- basics -------------------------------------------------------------

    def test_initialize_declares_capabilities(self):
        r = self.session.initialize()
        self.assertEqual(r["result"]["protocolVersion"], "2026-07-28")
        self.assertEqual(r["result"]["capabilities"]["prompts"], {"listChanged": True})
        self.assertEqual(r["result"]["capabilities"]["completions"], {})

    def test_ping(self):
        self.session.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(self.session.recv()["result"], {"resultType": "complete"})

    def test_prompts_list_empty_dir(self):
        self.session.initialize()
        self.session.send({"jsonrpc": "2.0", "id": 2, "method": "prompts/list"})
        r = self.session.recv()
        self.assertEqual(r["result"], {"resultType": "complete", "prompts": []})

    def test_prompts_list_parses_templates(self):
        self.write("code_review.md", REVIEW)
        self.write("plain.md", PLAIN)
        self.session.initialize()
        self.session.send({"jsonrpc": "2.0", "id": 3, "method": "prompts/list"})
        prompts = {p["name"]: p for p in self.session.recv()["result"]["prompts"]}
        self.assertEqual(set(prompts), {"code_review", "plain"})
        cr = prompts["code_review"]
        self.assertEqual(cr["title"], "Request Code Review")
        self.assertEqual(cr["description"], "Asks the LLM to analyze code quality.")
        self.assertEqual(cr["arguments"], [
            {"name": "code", "description": "The code to review", "required": True},
            {"name": "language", "description": "Programming language", "required": False},
        ])
        self.assertEqual(prompts["plain"]["description"], "A description line here.")
        self.assertNotIn("arguments", prompts["plain"])

    def test_bundled_examples_load(self):
        self.session.close(expect_exit=True)
        self.session = Session(EXAMPLES)
        self.session.initialize()
        self.session.send({"jsonrpc": "2.0", "id": 4, "method": "prompts/list"})
        prompts = {p["name"]: p for p in self.session.recv()["result"]["prompts"]}
        self.assertIn("code_review", prompts)
        self.assertIn("summarize", prompts)
        self.assertIn("explain", prompts)

    # -- prompts/get --------------------------------------------------------

    def test_get_substitutes_arguments(self):
        self.write("code_review.md", REVIEW)
        self.session.initialize()
        self.session.send({"jsonrpc": "2.0", "id": 5, "method": "prompts/get",
                           "params": {"name": "code_review",
                                      "arguments": {"code": "print(1)", "language": "python"}}})
        r = self.session.recv()["result"]
        self.assertEqual(r["resultType"], "complete")
        self.assertEqual(r["description"], "Asks the LLM to analyze code quality.")
        msg = r["messages"][0]
        self.assertEqual(msg["role"], "user")
        self.assertEqual(msg["content"]["type"], "text")
        self.assertIn("Please review this python code:", msg["content"]["text"])
        self.assertTrue(msg["content"]["text"].endswith("print(1)"))

    def test_get_missing_required_argument_rejected(self):
        self.write("code_review.md", REVIEW)
        self.session.initialize()
        self.session.send({"jsonrpc": "2.0", "id": 6, "method": "prompts/get",
                           "params": {"name": "code_review"}})
        r = self.session.recv()
        self.assertEqual(r["error"]["code"], -32602)
        self.assertIn("code", r["error"]["message"])

    def test_get_optional_argument_defaults_to_empty(self):
        self.write("code_review.md", REVIEW)
        self.session.initialize()
        self.session.send({"jsonrpc": "2.0", "id": 7, "method": "prompts/get",
                           "params": {"name": "code_review", "arguments": {"code": "x"}}})
        text = self.session.recv()["result"]["messages"][0]["content"]["text"]
        self.assertIn("Please review this  code:", text)

    def test_get_unknown_prompt_rejected(self):
        self.session.initialize()
        self.session.send({"jsonrpc": "2.0", "id": 8, "method": "prompts/get",
                           "params": {"name": "nope"}})
        self.assertEqual(self.session.recv()["error"]["code"], -32602)

    def test_get_no_args_template(self):
        self.write("plain.md", PLAIN)
        self.session.initialize()
        self.session.send({"jsonrpc": "2.0", "id": 9, "method": "prompts/get",
                           "params": {"name": "plain"}})
        text = self.session.recv()["result"]["messages"][0]["content"]["text"]
        self.assertEqual(text, "Just a fixed body with no placeholders.")

    def test_get_invalid_params(self):
        self.session.initialize()
        self.session.send({"jsonrpc": "2.0", "id": 10, "method": "prompts/get",
                           "params": {"arguments": {}}})
        self.assertEqual(self.session.recv()["error"]["code"], -32602)
        self.session.send({"jsonrpc": "2.0", "id": 11, "method": "prompts/get",
                           "params": {"name": "x", "arguments": {"k": 5}}})
        self.assertEqual(self.session.recv()["error"]["code"], -32602)

    def test_unknown_method_and_parse_error(self):
        self.session.initialize()
        self.session.send({"jsonrpc": "2.0", "id": 12, "method": "bogus/method"})
        self.assertEqual(self.session.recv()["error"]["code"], -32601)
        self.session.proc.stdin.write("{oops\n")
        self.session.proc.stdin.flush()
        self.assertEqual(self.session.recv()["error"]["code"], -32700)

    # -- completion ---------------------------------------------------------

    def test_completion_filters_enum_by_prefix(self):
        self.write("code_review.md", REVIEW)
        self.session.initialize()
        self.session.send({"jsonrpc": "2.0", "id": 13, "method": "completion/complete",
                           "params": {"ref": {"type": "ref/prompt", "name": "code_review"},
                                      "argument": {"name": "language", "value": "ja"}}})
        comp = self.session.recv()["result"]["completion"]
        self.assertEqual(comp, {"values": ["javascript"], "total": 1, "hasMore": False})

    def test_completion_empty_prefix_returns_all_choices(self):
        self.write("summarize.md", SUMMARIZE)
        self.session.initialize()
        self.session.send({"jsonrpc": "2.0", "id": 14, "method": "completion/complete",
                           "params": {"ref": {"type": "ref/prompt", "name": "summarize"},
                                      "argument": {"name": "style", "value": ""}}})
        comp = self.session.recv()["result"]["completion"]
        self.assertEqual(comp["values"], ["brief", "detailed", "bullets"])
        self.assertEqual(comp["total"], 3)

    def test_completion_argument_without_enum_is_empty(self):
        self.write("code_review.md", REVIEW)
        self.session.initialize()
        self.session.send({"jsonrpc": "2.0", "id": 15, "method": "completion/complete",
                           "params": {"ref": {"type": "ref/prompt", "name": "code_review"},
                                      "argument": {"name": "code", "value": "de"}}})
        self.assertEqual(self.session.recv()["result"]["completion"]["values"], [])

    def test_completion_rejects_bad_params(self):
        self.write("code_review.md", REVIEW)
        self.session.initialize()
        self.session.send({"jsonrpc": "2.0", "id": 16, "method": "completion/complete",
                           "params": {"ref": {"type": "ref/resource", "uri": "file:///x"},
                                      "argument": {"name": "a", "value": ""}}})
        self.assertEqual(self.session.recv()["error"]["code"], -32602)
        self.session.send({"jsonrpc": "2.0", "id": 17, "method": "completion/complete",
                           "params": {"ref": {"type": "ref/prompt", "name": "ghost"},
                                      "argument": {"name": "a", "value": ""}}})
        self.assertEqual(self.session.recv()["error"]["code"], -32602)

    # -- subscriptions / listChanged ---------------------------------------

    def test_listen_ack_honors_prompts_list_changed_only(self):
        self.session.initialize()
        self.session.send({"jsonrpc": "2.0", "id": 20, "method": "subscriptions/listen",
                           "params": {"notifications": {
                               "promptsListChanged": True,
                               "toolsListChanged": True,
                               "resourcesListChanged": True,
                               "resourceSubscriptions": ["file:///tmp/x"],
                           }}})
        ack = self.session.recv()
        self.assertEqual(ack["method"], "notifications/subscriptions/acknowledged")
        self.assertEqual(ack["params"]["_meta"][SUB_ID], 20)
        self.assertEqual(ack["params"]["notifications"], {"promptsListChanged": True})

    def test_list_changed_on_template_add_and_remove(self):
        self.session.initialize()
        self.session.send({"jsonrpc": "2.0", "id": 21, "method": "subscriptions/listen",
                           "params": {"notifications": {"promptsListChanged": True}}})
        self.assertEqual(self.session.recv()["method"], "notifications/subscriptions/acknowledged")
        self.settle()
        self.write("new_prompt.md", PLAIN)
        note = self.session.recv()
        self.assertEqual(note["method"], "notifications/prompts/list_changed")
        self.assertEqual(note["params"]["_meta"][SUB_ID], 21)
        self.settle(0.2)
        os.remove(os.path.join(self.root, "new_prompt.md"))
        note = self.session.recv()
        self.assertEqual(note["method"], "notifications/prompts/list_changed")

    def test_content_edit_also_triggers_list_changed(self):
        self.write("edit_me.md", PLAIN)
        self.session.initialize()
        self.session.send({"jsonrpc": "2.0", "id": 22, "method": "subscriptions/listen",
                           "params": {"notifications": {"promptsListChanged": True}}})
        self.session.recv()
        self.settle()
        self.write("edit_me.md", PLAIN + "\nMore body.\n")
        note = self.session.recv()
        self.assertEqual(note["method"], "notifications/prompts/list_changed")

    def test_no_subscription_means_no_notifications(self):
        self.session.initialize()
        self.settle()
        self.write("late.md", PLAIN)
        time.sleep(0.4)
        self.assertEqual(self.session.drain(0.2), [])

    def test_cancel_stops_notifications_and_completes(self):
        self.session.initialize()
        self.session.send({"jsonrpc": "2.0", "id": 23, "method": "subscriptions/listen",
                           "params": {"notifications": {"promptsListChanged": True}}})
        self.session.recv()
        self.settle()
        self.session.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                           "params": {"requestId": 23}})
        resp = self.session.recv()
        self.assertEqual(resp["id"], 23)
        self.assertEqual(resp["result"]["resultType"], "complete")
        self.assertEqual(resp["result"]["_meta"][SUB_ID], 23)
        self.settle()
        self.write("after.md", PLAIN)
        time.sleep(0.4)
        self.assertEqual(self.session.drain(0.2), [])

    def test_graceful_closure_on_stdin_eof(self):
        self.session.initialize()
        self.session.send({"jsonrpc": "2.0", "id": 24, "method": "subscriptions/listen",
                           "params": {"notifications": {"promptsListChanged": True}}})
        self.session.recv()
        self.session.proc.stdin.close()
        resp = self.session.recv(timeout=5)
        self.assertEqual(resp["id"], 24)
        self.assertEqual(resp["result"]["resultType"], "complete")
        self.assertIsNone(self.session.recv(timeout=5))
        self.session.proc.wait(timeout=10)
        self.assertEqual(self.session.proc.returncode, 0)
        self.assertEqual(self.session.proc.stderr.read(), "")

    def test_invalid_listen_params(self):
        self.session.initialize()
        for rid, nf in [(30, "nope"), (31, {"promptsListChanged": "yes"}),
                        (32, {"resourceSubscriptions": "x"})]:
            self.session.send({"jsonrpc": "2.0", "id": rid, "method": "subscriptions/listen",
                               "params": {"notifications": nf}})
            self.assertEqual(self.session.recv()["error"]["code"], -32602, "id=%r" % rid)


if __name__ == "__main__":
    unittest.main()
