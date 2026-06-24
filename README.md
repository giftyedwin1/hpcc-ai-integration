# HPCC AI Integration

Connects HPCC Systems clusters to AI assistants for natural-language → ECL
workflows. The core is a Model Context Protocol (MCP) server exposing HPCC tools;
on top of it are ready-to-use "ECL Expert" configurations for two clients.

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
└── .kiro/             # Kiro CLI integration (native discovery location)
    ├── agents/ecl-expert.json     # launch: kiro-cli chat --agent ecl-expert
    └── skills/ecl-expert/SKILL.md # referenced by the agent via skill://
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

## What's shared vs client-specific

- **Shared:** the MCP server (`mcp-server/`) — both clients launch the same
  `server.py` and get the same 16 HPCC tools.
- **Client-specific:** the "ECL Expert" skill/agent. Quick and Kiro use different
  skill formats and tool-naming conventions, so each has its own copy:
  - Quick: markdown-header `SKILL.md`, `hpcc_cluster__<tool>` names, `load_skill`.
  - Kiro: YAML-frontmatter `SKILL.md`, `@hpcc/<tool>` names, loaded via `skill://`.

> Note: Amazon Quick's MCP client uses **tools only** (resources/prompts are
> ignored), so the Quick experience is delivered through the imported Skill.
> Kiro CLI supports the full MCP protocol plus its own agents and skills.
