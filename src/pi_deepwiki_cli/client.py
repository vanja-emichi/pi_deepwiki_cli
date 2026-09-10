"""Thin direct client for DeepWiki's public Streamable HTTP endpoint.

No MCP SDK. The endpoint speaks plain JSON-RPC 2.0 over HTTP POST
(stateless — a single ``tools/call`` per invocation, no handshake, no
session), with responses framed either as JSON or as SSE ``data:`` lines.
Both are parsed here.

Endpoint facts (source: https://mcp.deepwiki.com/ and
https://docs.devin.ai/work-with-devin/deepwiki-mcp):

* Public repos: ``https://mcp.deepwiki.com/mcp`` — free, no auth.
* SSE (``/sse``) is legacy and deprecated — not used.

Private-repository access (mcp.devin.ai, Bearer auth) is deliberately out
of scope for this CLI.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logging.getLogger("httpx").setLevel(logging.WARNING)

PUBLIC_MCP_URL = "https://mcp.deepwiki.com/mcp"
DEFAULT_MCP_URL = PUBLIC_MCP_URL  # backwards-compatible alias
DEFAULT_TIMEOUT = 120.0

_JSON_RPC_ID = 1  # stateless single-request mode: a fixed id is fine


class DeepWikiError(Exception):
    """Base exception; subclasses carry an ``exit_code`` for the CLI."""

    exit_code = 1


class ConnectionError(DeepWikiError):
    """Failed to connect to / timely reach the DeepWiki server."""

    exit_code = 3


class ToolError(DeepWikiError):
    """The DeepWiki server returned an error for the requested tool."""

    exit_code = 4


def parse_body(text: str) -> dict[str, Any] | None:
    """Parse a response body that is either plain JSON or SSE-framed JSON.

    MCP Streamable HTTP may answer ``application/json`` (one object) or
    ``text/event-stream`` (events whose ``data:`` lines each carry one JSON
    object). We prefer the message carrying a ``result`` or ``error`` for
    our request.
    """
    stripped = text.strip()
    if not stripped:
        return None
    if not stripped.startswith(("event:", "data:", ":", "\r")):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return None
    best: dict[str, Any] | None = None
    for line in stripped.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload:
            continue
        try:
            message = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        if "result" in message or "error" in message:
            if message.get("id") == _JSON_RPC_ID or best is None:
                best = message
    return best


def extract_text(message: dict[str, Any]) -> str:
    """Extract the text payload from a JSON-RPC response message.

    Raises ToolError for JSON-RPC errors and for tool-level ``isError``
    results (the wire format uses camelCase, per the MCP spec).
    """
    if "error" in message:
        err = message["error"]
        if isinstance(err, dict):
            raise ToolError(str(err.get("message") or err))
        raise ToolError(str(err))

    result = message.get("result")
    if not isinstance(result, dict):
        raise ToolError(f"Unexpected response shape: {json.dumps(message)[:200]}")
    if result.get("isError"):
        parts = [
            c.get("text", "")
            for c in result.get("content", [])
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        raise ToolError("\n".join(parts).strip() or "Unknown tool error")
    return "\n".join(
        c.get("text", "")
        for c in result.get("content", [])
        if isinstance(c, dict) and c.get("type") == "text"
    )


class DeepWikiClient:
    """Synchronous JSON-RPC client for DeepWiki (public endpoint)."""

    def __init__(
        self,
        base_url: str = PUBLIC_MCP_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url
        self.timeout = timeout

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """One stateless POST; returns the tool's text answer."""
        payload = {
            "jsonrpc": "2.0",
            "id": _JSON_RPC_ID,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        try:
            response = httpx.post(
                self.base_url,
                content=json.dumps(payload),
                headers=headers,
                timeout=self.timeout,
            )
        except httpx.TimeoutException as e:
            raise ConnectionError(
                f"Request to {self.base_url} timed out after {self.timeout:.0f}s"
            ) from e
        except httpx.TransportError as e:
            raise ConnectionError(
                f"Could not connect to DeepWiki server: {e}"
            ) from e

        if response.status_code != 200:
            raise ConnectionError(
                f"DeepWiki server returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )

        message = parse_body(response.text)
        if message is None:
            raise ToolError(f"Could not parse response: {response.text[:200]}")
        return extract_text(message)

    # ---- tool wrappers -------------------------------------------------

    def read_wiki_structure(self, repo_name: str) -> str:
        """Documentation structure (table of contents) for owner/repo."""
        return self.call_tool("read_wiki_structure", {"repoName": repo_name})

    def read_wiki_contents(self, repo_name: str) -> str:
        """Full documentation contents for owner/repo."""
        return self.call_tool("read_wiki_contents", {"repoName": repo_name})

    def ask_question(self, repo_name: str, question: str) -> str:
        """Ask a question about owner/repo."""
        return self.call_tool(
            "ask_question", {"repoName": repo_name, "question": question}
        )
