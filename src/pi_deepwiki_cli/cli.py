"""CLI entry point — designed around how the Pi coding agent consumes CLIs.

Agent contract (documented in README.md):

* stdout carries DATA only; stderr carries diagnostics and errors.
* ``--json`` emits one JSON object on stdout; on failure the error JSON goes
  to stderr and the exit code is non-zero.
* Exit codes: 0 ok, 1 unexpected, 2 usage, 3 connection/timeout,
  4 server/tool error (e.g. repository not indexed).
* Piped stdin is merged into ``ask`` questions (mirrors ``pi -p``).
* JSON auto-enables when Pi launches us (``AI_AGENT``/``PI_CODING_AGENT``
  is set) and stdout is not a TTY.
* No prompts, no spinners, no ANSI when piped; ``NO_COLOR`` honored.
* Shared flags work in BOTH positions: ``pi-deepwiki --json ask ...`` and
  ``pi-deepwiki ask ... --json`` — agents type both.

Fully synchronous: one stateless HTTP POST per invocation, no background
machinery, nothing to clean up.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Callable, NoReturn

import click

from pi_deepwiki_cli import __version__
from pi_deepwiki_cli.client import (
    DEFAULT_TIMEOUT,
    PUBLIC_MCP_URL,
    ConnectionError,
    DeepWikiClient,
    DeepWikiError,
    ToolError,
)

EXIT_OK = 0
EXIT_GENERAL = 1  # unexpected
EXIT_USAGE = 2  # bad arguments (click's native usage-error exit)
EXIT_CONNECTION = 3  # network / timeout
EXIT_TOOL = 4  # DeepWiki server returned an error


def validate_repo_name(ctx: click.Context, param: click.Parameter, value: str) -> str:
    """Validate repository name format."""
    if "/" not in value:
        raise click.BadParameter(
            f"Invalid repository format: '{value}'. Expected format: owner/repo (e.g., facebook/react)"
        )
    parts = value.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise click.BadParameter(
            f"Invalid repository format: '{value}'. Expected format: owner/repo (e.g., facebook/react)"
        )
    return value


def launched_by_agent() -> bool:
    """True when a coding agent (e.g. Pi) launched this process."""
    return bool(os.environ.get("AI_AGENT") or os.environ.get("PI_CODING_AGENT"))


def read_stdin_if_piped() -> str:
    """Read piped stdin without ever blocking on a TTY."""
    try:
        if sys.stdin is not None and not sys.stdin.isatty():
            return sys.stdin.read().strip()
    except Exception:
        pass
    return ""


def truncate(text: str, max_bytes: int) -> tuple[str, bool]:
    """Cap output at ``max_bytes`` (0 = unlimited). Returns (text, truncated)."""
    if max_bytes <= 0 or len(text.encode("utf-8")) <= max_bytes:
        return text, False
    encoded = text.encode("utf-8")[:max_bytes]
    return encoded.decode("utf-8", errors="ignore"), True


class _Ctx:
    """Shared options, resolved once at group level, overridable per command."""

    def __init__(self) -> None:
        self.json_output: bool = False
        self.timeout: float = DEFAULT_TIMEOUT
        self.url: str = PUBLIC_MCP_URL
        self.max_bytes: int = 0

    def apply_overrides(
        self,
        json_output: bool | None,
        timeout: float | None,
        url: str | None,
        max_bytes: int | None,
    ) -> None:
        """Merge subcommand-level flags over group-level (or default) values."""
        if json_output is not None:
            self.json_output = json_output
        if timeout is not None:
            self.timeout = timeout
        if url is not None:
            self.url = url
        if max_bytes is not None:
            self.max_bytes = max_bytes

    def make_client(self) -> DeepWikiClient:
        return DeepWikiClient(base_url=self.url, timeout=self.timeout)


def get_ctx(ctx: click.Context) -> _Ctx:
    return ctx.obj


def shared_options(f: Callable) -> Callable:
    """Flags accepted both before and after the subcommand."""
    f = click.option(
        "--json/--text",
        "json_output",
        default=None,
        help=(
            "JSON mode: one JSON object on stdout (errors on stderr, non-zero exit). "
            "Auto-enabled when a coding agent launched us (AI_AGENT/PI_CODING_AGENT) "
            "and stdout is not a TTY."
        ),
    )(f)
    f = click.option(
        "--timeout",
        type=float,
        default=None,
        help=f"Client-side deadline in seconds per call (default: {DEFAULT_TIMEOUT:.0f}).",
    )(f)
    f = click.option(
        "--url",
        default=None,
        envvar="DEEPWIKI_MCP_URL",
        help=f"DeepWiki endpoint (env: DEEPWIKI_MCP_URL; default: {PUBLIC_MCP_URL}).",
    )(f)
    f = click.option(
        "--max-bytes",
        type=int,
        default=None,
        help="Cap result size in bytes to protect context windows (default: unlimited).",
    )(f)
    return f


def emit(ctx: click.Context, command: str, repo: str, result: str, truncated: bool) -> None:
    """Write a successful result: data only, on stdout."""
    if get_ctx(ctx).json_output:
        payload = {
            "ok": True,
            "command": command,
            "repo": repo,
            "result": result,
            "truncated": truncated,
        }
        click.echo(json.dumps(payload, ensure_ascii=False))
        return
    out = result if not truncated else f"{result}\n[output truncated at request]"
    click.echo(out)


def fail(ctx: click.Context, exc: DeepWikiError) -> NoReturn:
    """Write a failure: error JSON/text to stderr, then exit non-zero."""
    code = getattr(exc, "exit_code", EXIT_GENERAL)
    message = str(exc)
    if get_ctx(ctx).json_output:
        payload = {
            "ok": False,
            "error": {"code": code, "type": type(exc).__name__, "message": message},
        }
        click.echo(json.dumps(payload, ensure_ascii=False), err=True)
    else:
        # click colorizes only when the stream is a TTY; piped output stays clean.
        click.secho("Error: ", fg="red", nl=False, err=True)
        click.echo(message, err=True)
    sys.exit(code)


@click.group()
@click.version_option(version=__version__, prog_name="pi-deepwiki")
@shared_options
@click.pass_context
def cli(
    ctx: click.Context,
    json_output: bool | None,
    timeout: float | None,
    url: str | None,
    max_bytes: int | None,
) -> None:
    """Query DeepWiki documentation for any public GitHub repository — agent-friendly.

    Built for the Pi coding agent: JSON mode, meaningful exit codes,
    stdout/stderr discipline, piped-stdin merging, and bounded timeouts.
    Shared flags work before or after the subcommand.

    \b
    Exit codes:
      0  success
      1  unexpected error
      2  usage error
      3  connection / timeout
      4  DeepWiki server error (e.g. repository not indexed)

    \b
    Examples:
      pi-deepwiki structure facebook/react
      pi-deepwiki contents vercel/next.js --max-bytes 50000
      pi-deepwiki --json ask facebook/react "What is Fiber?"
      cat notes.md | pi-deepwiki ask owner/repo "Refine this analysis"
    """
    state = _Ctx()
    if json_output is None:
        json_output = launched_by_agent() and not sys.stdout.isatty()
    state.apply_overrides(json_output, timeout, url, max_bytes)
    ctx.obj = state


def _run(ctx: click.Context, command: str, repo: str, invoke: Callable[[], str]) -> None:
    try:
        result = invoke()
    except DeepWikiError as e:
        fail(ctx, e)
    except Exception as e:  # never exit 0 on an unexpected failure
        fail(ctx, DeepWikiError(f"Unexpected error: {e}"))
    capped, was_truncated = truncate(result, get_ctx(ctx).max_bytes)
    emit(ctx, command, repo, capped, was_truncated)


@cli.command()
@shared_options
@click.argument("repo", callback=validate_repo_name)
@click.pass_context
def structure(
    ctx: click.Context,
    repo: str,
    json_output: bool | None,
    timeout: float | None,
    url: str | None,
    max_bytes: int | None,
) -> None:
    """Get documentation structure (table of contents) for a repository.

    REPO should be in format owner/repo (e.g., facebook/react)
    """
    state = get_ctx(ctx)
    state.apply_overrides(json_output, timeout, url, max_bytes)
    client = state.make_client()
    _run(ctx, "structure", repo, lambda: client.read_wiki_structure(repo))


@cli.command()
@shared_options
@click.argument("repo", callback=validate_repo_name)
@click.pass_context
def contents(
    ctx: click.Context,
    repo: str,
    json_output: bool | None,
    timeout: float | None,
    url: str | None,
    max_bytes: int | None,
) -> None:
    """Get full documentation contents for a repository.

    REPO should be in format owner/repo (e.g., facebook/react)

    Tip: use --max-bytes to protect context windows on large wikis.
    """
    state = get_ctx(ctx)
    state.apply_overrides(json_output, timeout, url, max_bytes)
    client = state.make_client()
    _run(ctx, "contents", repo, lambda: client.read_wiki_contents(repo))


@cli.command()
@shared_options
@click.argument("repo", callback=validate_repo_name)
@click.argument("question")
@click.pass_context
def ask(
    ctx: click.Context,
    repo: str,
    question: str,
    json_output: bool | None,
    timeout: float | None,
    url: str | None,
    max_bytes: int | None,
) -> None:
    """Ask a question about a repository.

    REPO should be in format owner/repo (e.g., facebook/react)

    QUESTION is your question (quote it if it contains spaces).

    Piped stdin is merged into the question — mirroring `pi -p`:

        cat context.md | pi-deepwiki ask owner/repo "Given this, what next?"
    """
    state = get_ctx(ctx)
    state.apply_overrides(json_output, timeout, url, max_bytes)
    extra = read_stdin_if_piped()
    if extra:
        question = f"{question}\n\n--- piped stdin ---\n{extra}"
    client = state.make_client()
    _run(ctx, "ask", repo, lambda: client.ask_question(repo, question))


def main() -> None:
    """Main entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
