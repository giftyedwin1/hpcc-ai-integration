# HPCC MCP Server for Amazon Quick

Connects Amazon Quick to a live HPCC cluster via ESP REST APIs.

## Setup

Requires Python 3.10+. Use a venv (Homebrew/macOS Python blocks global `pip install`).

```bash
python3 -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r requirements.txt        # or: pip install mcp httpx
```

## Run

```bash
# Start port-forwards (one per terminal; tmux/screen keeps them stable)
kubectl port-forward svc/eclwatch 8010:8010
kubectl port-forward svc/eclqueries 8002:8002

# Sanity check — should return cluster activity JSON
curl http://localhost:8010/WsSMC/Activity.json
```

## Register in Quick

**Settings → Capabilities → Connections → MCP → Add MCP Server**

- **Name:** `HPCC Cluster`
- **Command:** `/Users/edwgifty/Projects/hpcc-ai-integration/mcp-server/.venv/bin/python`
- **Args:** `/Users/edwgifty/Projects/hpcc-ai-integration/mcp-server/server.py`

Point **Command** at the venv Python (Windows: `.venv\Scripts\python.exe`), not a
global `python` — Quick runs the server in its own process and needs the venv to
find `mcp`/`httpx`. Save and restart the chat, then ask: *"List my workunits"*.

## NLP → ECL Demo

The point of the demo: type plain English in Quick, let the model write ECL, run it
on the cluster, and show the answer — in one step.

Quick's model handles the natural-language → ECL translation, then calls
**`hpcc_run_ecl`**, which submits the code, waits for completion, and returns the
output rows (or compile/runtime errors). No manual polling.

**Try these prompts in Quick:**

> "Using ECL, count how many even numbers there are between 1 and 100."

> "Write ECL to generate the first 15 Fibonacci numbers and run it."

> "In ECL, create a dataset of 1000 random integers and return the average."

A successful run returns clean rows:

```json
{ "wuid": "W2026...-143014", "state": "completed",
  "results": [ { "even_count": "50" } ] }
```

A bad query returns the ECL error instead, so the model can self-correct:

```json
{ "state": "failed",
  "errors": [ { "severity": "Error", "message": "Unknown identifier \"this\"" } ] }
```

> Tip: keep demo queries small so they finish within `hpcc_run_ecl`'s default
> 60s wait. For long jobs use `hpcc_submit_ecl` + `hpcc_workunit_status`.

## Tools

| Tool | Purpose |
|------|---------|
| `hpcc_run_ecl` | **Run ECL in one shot** — submit, wait, return results (NLP demo) |
| `hpcc_syntax_check` | Compile-only ECL validation — fast self-correction, no full run |
| `hpcc_list_workunits` | List recent jobs with state/timing |
| `hpcc_get_workunit` | Full details + ECL source |
| `hpcc_submit_ecl` | Submit ECL without waiting (returns a WUID) |
| `hpcc_workunit_status` | Poll job status until done |
| `hpcc_get_results` | Retrieve output rows of a completed job |
| `hpcc_abort_workunit` | Stop a running/queued job |
| `hpcc_delete_workunit` | Delete a workunit and its results (cleanup) |
| `hpcc_cluster_health` | Queue status, running jobs |
| `hpcc_topology` | Clusters, node counts, services |
| `hpcc_list_files` | Browse DFU logical files |
| `hpcc_describe_file` | Show a logical file's record structure / columns |
| `hpcc_preview_file` | Preview the first N rows of a logical file's data |
| `hpcc_list_queries` | Discover published Roxie queries |
| `hpcc_run_roxie_query` | Execute published Roxie queries |

## Prompts & Resources

Beyond tools, the server exposes MCP **prompts** and **resources** that clients
like Quick can surface directly.

**Prompt**

| Prompt | Purpose |
|--------|---------|
| `ecl_expert` | Turns the model into an ECL expert: encodes ECL syntax rules and the recommended `describe → syntax_check → run` workflow for natural-language→ECL. Takes an optional `task` argument to embed the request inline. |

**Resources**

| Resource URI | Contents |
|--------------|----------|
| `hpcc://files` | Catalog of DFU logical files (name, record count, size) |
| `hpcc://topology` | Thor/Roxie/hThor clusters and queue status |
| `hpcc://file/{logical_name}` | Record structure / columns + ECL record def for a specific logical file |

The catalog's first batch of files is also listed as individual
`hpcc://file/...` schema resources, but the template resolves for *any* logical
file name — so the model can pull a dataset's schema in as context before
writing ECL against it.

## Configuration

All connection settings are read from environment variables (with sensible
localhost defaults), so you can point at a real or authenticated cluster without
editing `server.py`:

| Variable | Default | Purpose |
|----------|---------|---------|
| `HPCC_ESP_URL` | `http://localhost:8010` | ECLWatch / WsWorkunits base URL |
| `HPCC_WSECL_URL` | `http://localhost:8002` | WsECL base URL (Roxie queries) |
| `HPCC_CLUSTER` | `thor` | Default target cluster |
| `HPCC_TIMEOUT` | `30.0` | HTTP timeout (seconds) |
| `HPCC_USER` | _(none)_ | Basic-auth username (enables auth when set) |
| `HPCC_PASSWORD` | _(none)_ | Basic-auth password |

In Quick, set these under the MCP server's environment configuration.

## Troubleshooting

| Issue | Fix |
|-------|-----|
| "Connection failed" | Port-forward dropped — restart it |
| Empty workunit list | Run some ECL jobs to populate history |
| Auth errors | Cluster needs credentials — set `HPCC_USER` / `HPCC_PASSWORD` |
| Timeout | Raise `HPCC_TIMEOUT` (seconds) |

If HPCC requires authentication, set the credentials as environment variables
(in Quick's MCP server environment config, or your shell) — no source edits needed:

```bash
export HPCC_USER=myuser
export HPCC_PASSWORD=mypassword
export HPCC_ESP_URL=https://my-cluster:18010   # if not on localhost
```
