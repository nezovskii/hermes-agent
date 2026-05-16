# Nia integration design

## Goal

Make Nia a first-class Hermes retrieval layer for external knowledge: GitHub repositories, documentation sites, papers, datasets, and web/deep research. Nia should not replace local file tools, browser automation, or durable memory.

## Routing policy

Use the cheapest authoritative layer first:

1. **Local workspace/files** → `search_files`, `read_file`, `patch`, `terminal`.
   - Use for files already present in the active workdir or explicitly provided paths.
2. **Hermes/Gbrain memory** → memory/session/brain tools.
   - Use for durable decisions, summaries, reports, and previously synthesized conclusions.
3. **Nia** → external retrieval and indexed source search.
   - Use when the target is a remote repo, documentation site, package, paper, dataset, or cross-source research corpus.
   - Use before broad web search when a source is likely already indexed or indexable.
4. **Web/browser** → current unknowns, sites that require live interaction, or UI/QA flows.
5. **Gstack** → browser/product QA, design review, benchmark/canary, shipping workflows.

Important synthesized results from Nia research should be saved back into durable memory/brain. Nia is retrieval; Gbrain remains source of truth for conclusions.

## Native toolset

Toolset name: `nia`. It is intentionally separate from the core default bundle: users/platforms enable it explicitly with `hermes tools enable nia` or `--toolsets nia`, and the runtime `check_fn` hides it when credentials are missing.

Initial tools:

- `nia_usage` — account limits and usage.
- `nia_repos` — repository actions: `list`, `index`, `status`, `tree`, `grep`, `read`, `delete`, `rename`.
- `nia_sources` — documentation/source actions: `list`, `index`, `get`, `resolve`, `sync`, `tree`, `ls`, `grep`, `read`, `delete`, `rename`.
- `nia_search` — `query`, `web`, `deep`, `universal` search modes.

Future tools after API stabilization:

- `nia_oracle` — async oracle jobs/status/results.
- `nia_tracer` — trace entities/claims across sources.
- `nia_contexts` — save/search reusable source contexts.
- `nia_mcp` — compatibility adapter, not the primary integration path.

## Secret handling

Credential resolution order:

1. `NIA_API_KEY` environment variable.
2. `~/.config/nia/api_key`.
3. Optional config field later, if Hermes adopts a central external-service credential schema.

Rules:

- Never expose the key in schemas, tool output, exceptions, logs, or PR/docs.
- Tool output must redact bearer tokens and common API-key assignment forms defensively.
- Tool availability is gated by a `check_fn` so Nia tools appear only when a key exists.

## Indexing privacy policy

Default posture: private and reversible.

- Repository indexing defaults to **private**: `add_as_global_source` omitted unless the caller explicitly passes `add_global=true`.
- Documentation/source indexing also defaults to private at the Hermes layer, even if Nia scripts historically defaulted differently.
- Destructive actions (`delete`) require explicit action names and source/repo identifiers; no bulk delete in the initial toolset.
- Public/global indexing should be opt-in and visible in the tool result.

## Output format

All tools return JSON strings with stable keys:

- Success: raw Nia JSON plus `_meta` with `service`, `action`, and redaction-safe request metadata where useful.
- Failure: `{"error": "...", "http_status": <int|null>}`.
- File reads may return raw text in `content` when Nia provides content.

Agents should cite source paths, URLs, repo refs, and line ranges when Nia returns them. Do not claim certainty beyond retrieved evidence.

## Bart/OpenClaw compatibility

For Bart/OpenClaw, keep the existing Nia skill scripts as a working bridge. The native Hermes toolset should share the same semantics so later adapters can be thin wrappers.

Recommended order:

1. Native Hermes `nia` toolset.
2. Document policy in skill/docs.
3. Optional local MCP server that wraps the same Python client if first-class tool discovery is needed outside Hermes.

## MCP adapter phase

MCP is phase 2/3, not phase 1. It does not decide routing policy and adds another failure mode. Build it only after native semantics stabilize.

Adapter should expose the same operations as the native toolset and reuse the same credential resolution, redaction, privacy defaults, and tests.

## Tests/checks

Minimum test coverage:

- Credential resolution: env key, fallback key file, missing key.
- Request builder: auth header set, key not leaked.
- Repository actions: list/index/read/grep/tree URL and body shape.
- Source actions: list/index/read/grep/tree URL and body shape.
- Search modes: query/web/deep/universal body shape.
- Error handling: HTTP errors and non-JSON bodies return redacted JSON errors.
- Toolset wiring: `nia` exists in `toolsets.py`, tools are registered and available when key check passes.

Targeted checks:

```bash
python -m pytest tests/tools/test_nia_tool.py -q -o 'addopts='
python -m pytest tests/tools/test_nia_tool.py tests/test_yuanbao_integration.py -q -o 'addopts='
python -m py_compile tools/nia_tool.py toolsets.py
```
