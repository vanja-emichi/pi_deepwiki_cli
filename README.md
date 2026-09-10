# pi_deepwiki_cli

**Agent-friendly DeepWiki CLI for any public GitHub repository — aligned to how the [Pi coding agent](https://github.com/earendil-works/pi) uses CLIs.**

[DeepWiki](https://deepwiki.com) provides AI-generated documentation for open source repositories. This CLI lets an agent (or a human) explore documentation structure, read contents, or ask questions about any repository — with a strict machine contract: JSON mode, meaningful exit codes, clean stdout/stderr separation, piped-stdin merging, and bounded timeouts.

## Architecture — a plain CLI, no MCP SDK

The tool speaks DeepWiki's public endpoint directly: **one stateless JSON-RPC 2.0 `POST tools/call` per invocation** (verified against [mcp.deepwiki.com](https://mcp.deepwiki.com/) — no handshake, no session), parsing the JSON-or-SSE-framed response itself. Two dependencies total (`click` + `httpx`), fully synchronous, nothing running in the background.

Adapted from [4rays/deepwiki-cli](https://github.com/4rays/deepwiki-cli) (MIT), which used the `mcp` Python SDK. Going SDK-free fixes and hardens:

- **Fixed masked errors** — the original raised errors inside anyio TaskGroup contexts, so users saw `unhandled errors in a TaskGroup` instead of the real server message; now you see *"Repository not found. Visit … to index it."*
- **Added client-side timeouts** — a slow server can no longer hang the CLI forever
- **No SDK version churn** — the `mcp` 1.x→2.x rename/shape breaks (`streamablehttp_client`, tuple sizes, `isError`/`is_error`, `httpx2`) simply cannot happen; the wire format is stable
- **Agent contract** — everything below

Scope is **public repositories only** (free, no authentication). Private-repo access via `mcp.devin.ai` (Bearer auth) is deliberately out of scope — see the [DeepWiki MCP docs](https://docs.devin.ai/work-with-devin/deepwiki-mcp) if you need it.

## Installation

```bash
# uvx (recommended)
uvx --from pi-deepwiki-cli pi-deepwiki --help

# pip
pip install pi-deepwiki-cli
```

## Usage

```bash
pi-deepwiki structure facebook/react                 # table of contents
pi-deepwiki contents vercel/next.js --max-bytes 50000 # full docs, capped
pi-deepwiki ask facebook/react "What is Fiber?"      # Q&A
pi-deepwiki ask langchain-ai/langchain "How do I create a chain?" --json
```

Piped stdin merges into the question (mirrors `pi -p`):

```bash
cat notes.md | pi-deepwiki ask owner/repo "Given this context, what should I check next?"
```

## Agent contract

Built for consumption by coding agents (Pi, Claude Code, etc.) and scripts. The model never sees a terminal — only captured text and an exit status — so this CLI is strict about them.

### Output modes

| Mode | Flag | stdout | stderr |
|------|------|--------|--------|
| text (default) | `--text` | result | diagnostics/errors |
| JSON | `--json` | one JSON object (data only) | error JSON + diagnostics |

JSON mode **auto-enables** when the process was launched by a coding agent (`AI_AGENT` or `PI_CODING_AGENT` is set) and stdout is not a TTY. Force either way with `--json` / `--text`.

Success (stdout, exit 0):

```json
{"ok": true, "command": "ask", "repo": "facebook/react", "result": "Fiber is …", "truncated": false}
```

Failure (stderr, non-zero exit; stdout stays empty):

```json
{"ok": false, "error": {"code": 4, "type": "ToolError", "message": "Repository not found. Visit https://deepwiki.com/a/b to index it."}}
```

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | success |
| `1` | unexpected error |
| `2` | usage error (bad arguments) |
| `3` | connection / timeout |
| `4` | DeepWiki server error (e.g. repository not indexed) |

Judge results by exit status, never by scanning output for words like "error".

### Environment

| Variable | Purpose |
|----------|---------|
| `DEEPWIKI_MCP_URL` | Override the endpoint (default `https://mcp.deepwiki.com/mcp`; for testing/proxies) |
| `AI_AGENT` / `PI_CODING_AGENT` | Set by Pi; enables JSON auto-detection |
| `NO_COLOR` | Suppress color (color is already auto-disabled when piped) |

### Flags

| Flag | Description |
|------|-------------|
| `--json` / `--text` | Output mode (auto-detects agent + pipe) |
| `--timeout <s>` | Client-side deadline per call (default 120) |
| `--url <url>` | Endpoint override |
| `--max-bytes <n>` | Cap result size to protect context windows (0 = unlimited) |
| `--version`, `--help` | stdout, exit 0 |

### Calling from a Pi extension

```ts
const result = await pi.exec(
  "pi-deepwiki",
  ["ask", "facebook/react", "What is Fiber?", "--json", "--max-bytes", "20000"],
  { signal, timeout: 120_000 },
);
let data: any = {};
try { data = JSON.parse(result.stdout); } catch {}
if (result.exitCode !== 0 || !data.ok) { /* inspect result.stderr */ }
```

Or straight from the shell tool:

```bash
pi-deepwiki ask pytorch/pytorch "How does torch.compile work?" --json; echo "exit=$?"
```

## Repository format

All commands expect `owner/repo`, e.g. `facebook/react`, `vercel/next.js`. If DeepWiki has not indexed the repository yet, the error message tells you exactly where to index it (exit code `4`).

## Development

```bash
git clone https://github.com/vanja-emichi/pi_deepwiki_cli
cd pi_deepwiki_cli
uv sync
uv run pytest
uv run pi-deepwiki --help
```

## Requirements

- Python 3.10+

## License

MIT — see [LICENSE](LICENSE). Adapted from [4rays/deepwiki-cli](https://github.com/4rays/deepwiki-cli) (MIT).
