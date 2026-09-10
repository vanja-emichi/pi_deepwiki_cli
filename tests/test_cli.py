"""Offline tests: wire-format parsing, exit codes, JSON contract, stdin merge."""

from __future__ import annotations

import json

import httpx
import pytest
from click.testing import CliRunner

from pi_deepwiki_cli import __version__
from pi_deepwiki_cli.cli import EXIT_CONNECTION, EXIT_TOOL, EXIT_USAGE, cli, validate_repo_name
from pi_deepwiki_cli.client import (
    ConnectionError,
    DeepWikiClient,
    DeepWikiError,
    ToolError,
    extract_text,
    parse_body,
)


# ---------- helpers ----------

def stderr_of(result) -> str:
    """click >=8.2 separates stderr; older versions may raise when mixed."""
    try:
        return result.stderr or ""
    except (ValueError, AttributeError):
        return ""


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def hook(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return "recorded-answer"


def patch_command(monkeypatch, method: str, outcome):
    """Patch DeepWikiClient.<method>; outcome is a return value or exception."""
    rec = _Recorder()

    def hook(self, *args, **kwargs):
        rec.calls.append((args, kwargs))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(DeepWikiClient, method, hook)
    return rec


# ---------- validation ----------

@pytest.mark.parametrize(
    "repo",
    ["facebook/react", "a/b", "vercel/next.js", "x/y.z"],
)
def test_validate_repo_name_accepts(repo):
    assert validate_repo_name(None, None, repo) == repo


@pytest.mark.parametrize("repo", ["facebook", "", "/react", "facebook/", "a/b/c", "/"])
def test_validate_repo_name_rejects(repo):
    import click as _click

    with pytest.raises(_click.BadParameter):
        validate_repo_name(None, None, repo)


def test_usage_error_exit_code(runner):
    result = runner.invoke(cli, ["structure", "not-a-repo"])
    assert result.exit_code == EXIT_USAGE


def test_version(runner):
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


# ---------- JSON contract ----------

def test_json_success_shape(runner, monkeypatch):
    patch_command(monkeypatch, "read_wiki_structure", "TOC-LINE")
    result = runner.invoke(cli, ["--json", "structure", "pytorch/pytorch"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {
        "ok": True,
        "command": "structure",
        "repo": "pytorch/pytorch",
        "result": "TOC-LINE",
        "truncated": False,
    }


def test_flags_work_in_both_positions(runner, monkeypatch):
    patch_command(monkeypatch, "read_wiki_structure", "X")
    before = runner.invoke(cli, ["--json", "structure", "a/b"])
    after = runner.invoke(cli, ["structure", "a/b", "--json"])
    assert before.exit_code == 0 and after.exit_code == 0
    assert json.loads(before.stdout)["result"] == "X"
    assert json.loads(after.stdout)["result"] == "X"


def test_json_failure_goes_to_stderr_nonzero(runner, monkeypatch):
    patch_command(
        monkeypatch,
        "read_wiki_structure",
        ToolError("Repository not found. Visit https://deepwiki.com/a/b to index it."),
    )
    result = runner.invoke(cli, ["--json", "structure", "a/b"])
    assert result.exit_code == EXIT_TOOL
    err = json.loads(stderr_of(result))
    assert err["ok"] is False
    assert err["error"]["code"] == EXIT_TOOL
    assert "Repository not found" in err["error"]["message"]
    assert result.stdout.strip() == ""  # stdout stays clean on failure


def test_connection_error_exit_code(runner, monkeypatch):
    patch_command(monkeypatch, "ask_question", ConnectionError("Could not connect"))
    result = runner.invoke(cli, ["--json", "ask", "a/b", "q"])
    assert result.exit_code == EXIT_CONNECTION


def test_unexpected_error_never_exits_zero(runner, monkeypatch):
    patch_command(monkeypatch, "ask_question", RuntimeError("boom"))
    result = runner.invoke(cli, ["--json", "ask", "a/b", "q"])
    assert result.exit_code == 1


# ---------- text mode ----------

def test_text_mode_plain_stdout(runner, monkeypatch):
    patch_command(monkeypatch, "ask_question", "plain answer")
    result = runner.invoke(cli, ["--text", "ask", "a/b", "what?"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "plain answer"


def test_truncation_marks_output(runner, monkeypatch):
    patch_command(monkeypatch, "read_wiki_contents", "x" * 1000)
    result = runner.invoke(cli, ["--text", "--max-bytes", "10", "contents", "a/b"])
    assert result.exit_code == 0
    assert result.stdout.startswith("x" * 10)
    assert "[output truncated" in result.stdout
    assert len(result.stdout) < 100


def test_truncation_json_flag(runner, monkeypatch):
    patch_command(monkeypatch, "read_wiki_contents", "x" * 1000)
    result = runner.invoke(cli, ["--json", "--max-bytes", "10", "contents", "a/b"])
    payload = json.loads(result.stdout)
    assert payload["truncated"] is True
    assert payload["result"] == "x" * 10


# ---------- stdin merge ----------

def test_ask_merges_piped_stdin(runner, monkeypatch):
    rec = patch_command(monkeypatch, "ask_question", "ok")
    result = runner.invoke(
        cli, ["ask", "a/b", "refine this"], input="extra context from stdin"
    )
    assert result.exit_code == 0
    sent_question = rec.calls[0][0][1]
    assert "refine this" in sent_question
    assert "--- piped stdin ---" in sent_question
    assert "extra context from stdin" in sent_question


def test_ask_without_stdin_untouched(runner, monkeypatch):
    rec = patch_command(monkeypatch, "ask_question", "ok")
    result = runner.invoke(cli, ["ask", "a/b", "plain question"], input="")
    assert result.exit_code == 0
    assert rec.calls[0][0][1] == "plain question"


# ---------- endpoint resolution ----------

def test_default_endpoint_is_public(runner, monkeypatch):
    seen = {}

    def hook(self, repo_name):
        seen["url"] = self.base_url
        return "ok"

    monkeypatch.setattr(DeepWikiClient, "read_wiki_structure", hook)
    result = runner.invoke(cli, ["structure", "a/b"])
    assert result.exit_code == 0
    assert seen["url"] == "https://mcp.deepwiki.com/mcp"


def test_url_override_reaches_client(runner, monkeypatch):
    seen = {}

    def hook(self, repo_name):
        seen["url"] = self.base_url
        return "ok"

    monkeypatch.setattr(DeepWikiClient, "read_wiki_structure", hook)
    result = runner.invoke(cli, ["structure", "a/b", "--url", "http://localhost:9/mcp"])
    assert result.exit_code == 0
    assert seen["url"] == "http://localhost:9/mcp"


# ---------- wire-format parsing ----------

def test_parse_body_plain_json():
    msg = {"jsonrpc": "2.0", "id": 1, "result": {"content": []}}
    assert parse_body(json.dumps(msg)) == msg


def test_parse_body_sse_framed():
    body = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"hi"}]}}\n\n'
    parsed = parse_body(body)
    assert parsed is not None
    assert extract_text(parsed) == "hi"


def test_parse_body_empty_and_garbage():
    assert parse_body("") is None
    assert parse_body("not json at all") is None


def test_extract_text_joins_content():
    msg = {
        "result": {
            "content": [
                {"type": "text", "text": "part 1"},
                {"type": "text", "text": "part 2"},
            ]
        }
    }
    assert extract_text(msg) == "part 1\npart 2"


def test_extract_text_server_error_message():
    msg = {
        "result": {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": "Error fetching wiki for a/b: Repository not found. Visit https://deepwiki.com/a/b to index it.",
                }
            ],
        }
    }
    with pytest.raises(ToolError, match="Repository not found"):
        extract_text(msg)


def test_extract_text_jsonrpc_error_object():
    msg = {"error": {"code": -32601, "message": "Method not found"}}
    with pytest.raises(ToolError, match="Method not found"):
        extract_text(msg)


def test_extract_text_unexpected_shape():
    with pytest.raises(ToolError, match="Unexpected response shape"):
        extract_text({"jsonrpc": "2.0", "id": 1})


# ---------- client transport (monkeypatched httpx.post) ----------

class _FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


def test_call_tool_success(monkeypatch):
    body = (
        "event: message\n"
        'data: {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"ANSWER"}]}}\n\n'
    )
    captured = {}

    def fake_post(url, content=None, headers=None, timeout=None):
        captured.update(url=url, content=content, headers=headers)
        return _FakeResponse(200, body)

    monkeypatch.setattr(httpx, "post", fake_post)
    assert DeepWikiClient().call_tool("ask_question", {"repoName": "a/b", "question": "q"}) == "ANSWER"
    sent = json.loads(captured["content"])
    assert sent["method"] == "tools/call"
    assert sent["params"]["name"] == "ask_question"
    assert captured["headers"]["Accept"] == "application/json, text/event-stream"
    assert "Authorization" not in captured["headers"]


def test_call_tool_timeout(monkeypatch):
    def fake_post(*a, **k):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(ConnectionError, match="timed out"):
        DeepWikiClient(timeout=5).call_tool("read_wiki_structure", {"repoName": "a/b"})


def test_call_tool_http_error(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(503, "busy"))
    with pytest.raises(ConnectionError, match="503"):
        DeepWikiClient().call_tool("read_wiki_structure", {"repoName": "a/b"})


# ---------- error hierarchy ----------

def test_error_hierarchy():
    assert issubclass(ConnectionError, DeepWikiError)
    assert issubclass(ToolError, DeepWikiError)
    assert ConnectionError.exit_code == EXIT_CONNECTION
    assert ToolError.exit_code == EXIT_TOOL
