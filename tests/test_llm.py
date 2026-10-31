import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace as NS

from src.labels import judge
from src.llm import CliBackend, LLMError, SdkBackend, cli_env, extract_json, render_conversation

TOOLS = [{"name": "get_product", "description": "d", "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}}]
HISTORY = [
    {"role": "user", "content": "[agora: …]\nQuanto custa o GD20?"},
    {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "get_product", "input": {"id_or_name": "GD20"}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{\"preco\": \"R$ 2.199,00\"}"}]},
]


class FakeMessages:
    def __init__(self, response):
        self.response, self.kwargs = response, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


class SdkTest(unittest.TestCase):
    def test_maps_blocks_and_caches_system(self):
        resp = NS(content=[NS(type="text", text="Vou ver."), NS(type="tool_use", id="tu1", name="get_product", input={"id_or_name": "GD20"})],
                  usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=100, cache_creation_input_tokens=0),
                  model="claude-sonnet-5-20261001")
        fake = FakeMessages(resp)
        turn = SdkBackend("k", "claude-sonnet-5", client=NS(messages=fake)).call("SYS", HISTORY, TOOLS)
        self.assertEqual(turn.text, "Vou ver.")
        self.assertEqual(turn.model, "claude-sonnet-5-20261001")
        self.assertEqual([(c.id, c.name, c.input) for c in turn.tool_calls], [("tu1", "get_product", {"id_or_name": "GD20"})])
        self.assertEqual(turn.content[1], {"type": "tool_use", "id": "tu1", "name": "get_product", "input": {"id_or_name": "GD20"}})
        self.assertEqual((turn.usage.input, turn.usage.cache_read), (10, 100))
        self.assertEqual(fake.kwargs["system"], [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}])
        self.assertEqual((fake.kwargs["model"], fake.kwargs["tools"], fake.kwargs["messages"]), ("claude-sonnet-5", TOOLS, HISTORY))


def envelope(result):
    return json.dumps({"result": result, "usage": {"input_tokens": 3, "output_tokens": 4}})


class FakeRunner:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, stdout=envelope(self.results.pop(0)), stderr="")


class CliTest(unittest.TestCase):
    def test_flags_env_and_tool_calls(self):
        runner = FakeRunner('{"text": null, "tool_calls": [{"name": "get_product", "input": {"id_or_name": "GD20"}}]}')
        turn = CliBackend("sonnet", runner=runner).call("SYS", HISTORY, TOOLS)
        argv, kw = runner.calls[0]
        self.assertEqual(argv[:2], ["claude", "-p"])
        for flag in ["--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"]:
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--system-prompt") + 1], "SYS")
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(kw["env"]["MAX_THINKING_TOKENS"], "0")
        self.assertIsNone(turn.text)
        self.assertIsNone(turn.model)
        self.assertEqual([(c.name, c.input) for c in turn.tool_calls], [("get_product", {"id_or_name": "GD20"})])
        self.assertEqual(turn.content, [{"type": "tool_use", "id": turn.tool_calls[0].id, "name": "get_product", "input": {"id_or_name": "GD20"}}])
        self.assertIn("get_product", argv[-1])  # tool list is in the prompt

    def test_retries_once_on_bad_json(self):
        runner = FakeRunner("not json", '{"text": "Oi!", "tool_calls": []}')
        self.assertEqual(CliBackend("sonnet", runner=runner).call("SYS", HISTORY, TOOLS).text, "Oi!")
        self.assertEqual(len(runner.calls), 2)

    def test_text_reply_is_kept_in_content_and_history(self):
        turn = CliBackend("sonnet", runner=FakeRunner('{"text": "Oi! Tudo bem?", "tool_calls": []}')).call("SYS", HISTORY, TOOLS)
        self.assertEqual(turn.content, [{"type": "text", "text": "Oi! Tudo bem?"}])
        history = HISTORY + [{"role": "assistant", "content": turn.content}, {"role": "user", "content": "E no PIX?"}]
        self.assertIn("VOCÊ: Oi! Tudo bem?", render_conversation(history))

    def test_gives_up_after_two_bad_replies(self):
        with self.assertRaises(LLMError):
            CliBackend("sonnet", runner=FakeRunner("x", "y")).call("SYS", HISTORY, TOOLS)

    def test_toolless_rubric_reply_is_plain_text(self):
        reply = '```json\n{"resolution": "resolved", "resolution_evidence": "Oi", "frustration": 0, "frustration_evidence": ""}\n```'
        turn = CliBackend("haiku", runner=FakeRunner(reply)).call("SYS", [{"role": "user", "content": "CLIENTE: Oi"}], [])
        self.assertEqual(turn.text, reply)
        self.assertEqual(turn.tool_calls, [])
        self.assertEqual(turn.content, [{"type": "text", "text": reply}])
        label = judge([{"kind": "customer", "text": "Oi"}], CliBackend("haiku", runner=FakeRunner(reply)))
        self.assertEqual((label["resolution"], label["frustration"]), ("resolved", 0))
        self.assertTrue(label["resolution_evidence_found"])

    def test_toolless_envelope_reply_still_reads_text(self):
        turn = CliBackend("haiku", runner=FakeRunner('{"text": "Oi!", "tool_calls": []}')).call("SYS", HISTORY, [])
        self.assertEqual(turn.text, "Oi!")

    def test_env_drops_api_key_and_effort(self):
        env = cli_env({"PATH": "/bin", "ANTHROPIC_API_KEY": "k", "CLAUDE_EFFORT": "high"})
        self.assertEqual(env, {"PATH": "/bin", "MAX_THINKING_TOKENS": "0"})

    def test_env_sets_home_when_given(self):
        env = cli_env({"PATH": "/bin", "HOME": "/home/me"}, home="/tmp/empty")
        self.assertEqual((env["HOME"], env["USERPROFILE"]), ("/tmp/empty", "/tmp/empty"))
        self.assertEqual(cli_env({"HOME": "/home/me"})["HOME"], "/home/me")

    def test_env_with_home_drops_config_dir(self):
        self.assertNotIn("CLAUDE_CONFIG_DIR", cli_env({"CLAUDE_CONFIG_DIR": "/home/me/.claude"}, home="/tmp/empty"))
        self.assertEqual(cli_env({"CLAUDE_CONFIG_DIR": "/home/me/.claude"})["CLAUDE_CONFIG_DIR"], "/home/me/.claude")

    def test_empty_token_keeps_real_home(self):
        reply = '{"text": "Oi!", "tool_calls": []}'
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "", "HOME": "/home/me"}):
            runner = FakeRunner(reply)
            CliBackend("sonnet", runner=runner).call("SYS", HISTORY, TOOLS)
        self.assertEqual(runner.calls[0][1]["env"]["HOME"], "/home/me")

    def test_empty_home_is_removed_at_exit(self):
        reply = '{"text": "Oi!", "tool_calls": []}'
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "t"}), \
                mock.patch("src.llm.atexit.register") as register:
            runner = FakeRunner(reply)
            CliBackend("sonnet", runner=runner).call("SYS", HISTORY, TOOLS)
        home = runner.calls[0][1]["env"]["HOME"]
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        register.assert_called_once_with(shutil.rmtree, home, ignore_errors=True)

    def test_empty_home_only_with_token(self):
        reply = '{"text": "Oi!", "tool_calls": []}'
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "t", "HOME": "/home/me"}):
            runner = FakeRunner(reply, reply)
            backend = CliBackend("sonnet", runner=runner)
            backend.call("SYS", HISTORY, TOOLS)
            backend.call("SYS", HISTORY, TOOLS)
        homes = [kw["env"]["HOME"] for _, kw in runner.calls]
        self.assertTrue(Path(homes[0]).name.startswith("duda-cli-home-"))
        self.addCleanup(os.rmdir, homes[0])
        self.assertEqual(homes[0], homes[1])
        self.assertEqual(os.listdir(homes[0]), [])
        self.assertEqual(runner.calls[0][1]["env"]["USERPROFILE"], homes[0])

        env_without = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
        env_without["HOME"] = "/home/me"
        with mock.patch.dict(os.environ, env_without, clear=True):
            runner = FakeRunner(reply)
            CliBackend("sonnet", runner=runner).call("SYS", HISTORY, TOOLS)
        self.assertEqual(runner.calls[0][1]["env"]["HOME"], "/home/me")
        self.assertEqual(runner.calls[0][1]["env"].get("USERPROFILE"), env_without.get("USERPROFILE"))

    def test_render_and_extract(self):
        text = render_conversation(HISTORY)
        self.assertIn("CLIENTE: [agora: …]\nQuanto custa o GD20?", text)
        self.assertIn('VOCÊ CHAMOU get_product {"id_or_name": "GD20"}', text)
        self.assertIn('RESULTADO get_product: {"preco": "R$ 2.199,00"}', text)
        self.assertEqual(extract_json('lixo {"a": 1} fim'), {"a": 1})
