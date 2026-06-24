# HPCC AI Integration

Connects HPCC Systems clusters to AI assistants for natural-language → ECL
workflows. The core is a Model Context Protocol (MCP) server exposing HPCC tools;
on top of it are ready-to-use "ECL Expert" configurations for three clients.

## Layout

```
hpcc-ai-integration/
├── mcp-server/        # The MCP server (shared core) — start here
│   ├── server.py
│   ├── requirements.txt
│   └── README.md      # setup, configuration, tool/prompt/resource reference
│
├── quick/             # Amazon Quick Desktop integration
│   └── ecl-expert/
│       └── SKILL.md   # import via Agents & skills → Skills → Import from file
│
├── .kiro/             # Kiro CLI integration (native discovery location)
│   ├── agents/ecl-expert.json     # launch: kiro-cli chat --agent ecl-expert
│   └── skills/ecl-expert/SKILL.md # referenced by the agent via skill://
│
├── .github/agents/    # GitHub Copilot CLI custom agent (tested)
│   └── ecl-expert.agent.md        # select via /agents in `copilot`
└── .mcp.json          # Copilot CLI MCP server config (mcpServers + type: stdio)
```

## Quick start

1. **Set up the MCP server** — see [`mcp-server/README.md`](mcp-server/README.md)
   (create the venv, install deps, start the `kubectl` port-forwards).
2. **Pick your client:**
   - **Amazon Quick Desktop** — connect the MCP server under
     Settings → Capabilities → Connectors → MCP Servers, then import
     `quick/ecl-expert/SKILL.md` as a Skill.
   - **Kiro CLI** — run `kiro-cli chat --agent ecl-expert` from this directory
     (the agent in `.kiro/` embeds the server and references the skill).
   - **GitHub Copilot CLI** — run `copilot` from this directory. It picks up the
     `hpcc` server from `.mcp.json` and the agent from `.github/agents/`. Verify
     with `/mcp show` (lists `hpcc`), then `/agents` to select **ECL Expert**.
     Runs locally, so it reaches the same `kubectl` port-forwards as Kiro.

> **Pointing at a real cluster:** by default every client targets
> `http://localhost:8010` / `:8002`. To use a different cluster, set
> `HPCC_ESP_URL` and `HPCC_WSECL_URL` (and optionally `HPCC_CLUSTER`,
> `HPCC_USER`/`HPCC_PASSWORD`) in that client's `env`: `.mcp.json` (Copilot CLI),
> `.kiro/agents/ecl-expert.json` (Kiro), or the connector UI (Quick). All clients
> read these from the same `server.py`; see `mcp-server/README.md` for the full
> list. Avoid committing private cluster hostnames to a public repo.

## What's shared vs client-specific

- **Shared:** the MCP server (`mcp-server/`) — all clients launch the same
  `server.py` and get the same 16 HPCC tools.
- **Client-specific:** the "ECL Expert" skill/agent. Each client uses a different
  format and tool-naming convention, so each has its own copy:
  - Quick: markdown-header `SKILL.md`, `hpcc_cluster__<tool>` names, `load_skill`.
  - Kiro: YAML-frontmatter `SKILL.md`, `@hpcc/<tool>` names, loaded via `skill://`.
  - Copilot CLI: `.agent.md` agent (frontmatter + prompt body, no separate skill),
    `hpcc/*` tool refs, MCP server in `.mcp.json` (`mcpServers` key, `type: stdio`).

> Note: Amazon Quick's MCP client uses **tools only** (resources/prompts are
> ignored), so the Quick experience is delivered through the imported Skill.
> Kiro CLI and GitHub Copilot CLI support the full MCP protocol plus their own
> agents. The Copilot CLI agent runs locally (interactive, like Kiro), reaching
> the same port-forwarded cluster; it is not the GitHub.com cloud coding agent,
> which runs on a hosted runner and could not reach a local cluster.
