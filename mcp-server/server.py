r"""
HPCC Systems MCP Server
========================
Connects Amazon Quick to a live HPCC cluster via ESP REST APIs.

Setup:
  1. Create and activate a virtual environment:
       python3 -m venv .venv
       source .venv/bin/activate        # Windows: .venv\Scripts\activate
  2. pip install mcp httpx
  3. Ensure port-forwards are running:
     - kubectl port-forward svc/eclwatch 8010:8010
     - kubectl port-forward svc/eclqueries 8002:8002
  4. Run: python server.py

Register in Quick:
  Settings → Capabilities → Connections → MCP → Add
  Command: /path/to/.venv/bin/python   (Windows: .venv\Scripts\python.exe)
  Args:    /path/to/server.py
"""

import os
import json
import time
import asyncio
import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Tool,
    TextContent,
    Prompt,
    PromptArgument,
    PromptMessage,
    GetPromptResult,
    Resource,
)

# ─── Configuration ───────────────────────────────────────────────────────────
# All settings are overridable via environment variables so the server can point
# at a real / authenticated cluster without editing source.
ESP_BASE_URL = os.environ.get("HPCC_ESP_URL", "http://localhost:8010").rstrip("/")
WSECL_BASE_URL = os.environ.get("HPCC_WSECL_URL", "http://localhost:8002").rstrip("/")
DEFAULT_CLUSTER = os.environ.get("HPCC_CLUSTER", "thor")
HPCC_TIMEOUT = float(os.environ.get("HPCC_TIMEOUT", "30.0"))

# Basic-auth credentials (optional). Set HPCC_USER / HPCC_PASSWORD if the cluster
# requires authentication; otherwise requests are sent unauthenticated.
_HPCC_USER = os.environ.get("HPCC_USER", "")
_HPCC_PASSWORD = os.environ.get("HPCC_PASSWORD", "")
_AUTH = (_HPCC_USER, _HPCC_PASSWORD) if _HPCC_USER else None

# ─── HTTP Client ─────────────────────────────────────────────────────────────
# Async client so polling / multiple tool calls never block the event loop.
client = httpx.AsyncClient(timeout=HPCC_TIMEOUT, auth=_AUTH)

async def esp_get(path: str, params: dict = None) -> dict:
    """GET request to ESP REST API."""
    url = f"{ESP_BASE_URL}{path}"
    resp = await client.get(url, params=params or {})
    resp.raise_for_status()
    return resp.json()

async def esp_post(path: str, payload: dict) -> dict:
    """POST request to ESP REST API.

    ESP's JSON action endpoints require the payload wrapped in a
    `{"<Action>Request": {...}}` envelope (e.g. WUCreate -> WUCreateRequest);
    a bare JSON object is rejected with "End of stream encountered whilst
    parsing". Derive the envelope name from the path and wrap automatically.
    """
    url = f"{ESP_BASE_URL}{path}"
    action = path.rsplit("/", 1)[-1].split(".", 1)[0]  # "/WsWorkunits/WUCreate.json" -> "WUCreate"
    wrapper = f"{action}Request"
    body = payload if (len(payload) == 1 and wrapper in payload) else {wrapper: payload}
    resp = await client.post(url, json=body)
    resp.raise_for_status()
    return resp.json()


async def get_wu_state(wuid: str) -> str:
    """Return the current state of a workunit (e.g. 'completed', 'failed')."""
    data = await esp_get("/WsWorkunits/WUQuery.json", {"Wuid": wuid})
    wus = data.get("WUQueryResponse", {}).get("Workunits", {}).get("ECLWorkunit", [])
    return wus[0].get("State", "") if wus else "unknown"


async def fetch_result_rows(wuid: str, sequence: int = 0, count: int = 100) -> list:
    """Return clean output rows for a workunit result set (no XML schema noise)."""
    data = await esp_get("/WsWorkunits/WUResult.json", {
        "Wuid": wuid, "Sequence": sequence, "Count": count,
    })
    result = data.get("WUResultResponse", {}).get("Result", {}) or {}
    rows = result.get("Row", [])
    return rows if isinstance(rows, list) else [rows]


async def fetch_wu_exceptions(wuid: str) -> list:
    """Return ECL exceptions/errors for a workunit, if any."""
    data = await esp_get("/WsWorkunits/WUInfo.json", {"Wuid": wuid, "IncludeExceptions": "true"})
    wu = data.get("WUInfoResponse", {}).get("Workunit", {})
    excs = wu.get("Exceptions", {}).get("ECLException", [])
    excs = excs if isinstance(excs, list) else [excs]
    return [
        {"severity": e.get("Severity", ""), "message": e.get("Message", "")}
        for e in excs if isinstance(e, dict)
    ]


def as_list(value) -> list:
    """Normalize an ESP field that may be a single dict, a list, or absent.

    ESP returns a bare object when a collection has exactly one element, which
    otherwise breaks code that assumes a list. Use this everywhere we iterate
    ESP collections.
    """
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


import re as _re


def parse_ecl_record(ecl: str) -> list:
    """Parse a DFUInfo `Ecl` RECORD definition into [{name, type}] columns.

    Containerized HPCC leaves DFUInfo's structured `DataColumns` empty but still
    returns the ECL record text, so we derive columns from that instead.
    """
    if not ecl:
        return []
    m = _re.search(r"RECORD(.*?)\bEND\b", ecl, _re.S | _re.I)
    inner = m.group(1) if m else ecl
    columns = []
    for stmt in inner.split(";"):
        stmt = _re.sub(r"\{[^}]*\}", " ", stmt).strip()  # drop {...} field modifiers
        stmt = stmt.split("//", 1)[0].split(":=", 1)[0].strip()  # drop comments/defaults
        if not stmt:
            continue
        parts = stmt.split()
        if len(parts) >= 2:
            columns.append({"name": parts[-1], "type": parts[0]})
    return columns


async def run_ecl_and_wait(ecl_code: str, cluster: str, timeout_seconds: int,
                           job_name: str = "") -> dict:
    """Create -> update -> submit an ECL workunit and poll until terminal/timeout.

    Shared by hpcc_run_ecl and hpcc_preview_file. Returns {wuid, state}.
    """
    create_resp = await esp_post("/WsWorkunits/WUCreate.json", {})
    wuid = create_resp.get("WUCreateResponse", {}).get("Workunit", {}).get("Wuid")
    if not wuid:
        return {"wuid": None, "state": "error", "error": "Failed to create workunit"}

    update_payload = {"Wuid": wuid, "QueryText": ecl_code}
    if job_name:
        update_payload["Jobname"] = job_name
    await esp_post("/WsWorkunits/WUUpdate.json", update_payload)
    await esp_post("/WsWorkunits/WUSubmit.json", {"Wuid": wuid, "Cluster": cluster})

    terminal = {"completed", "failed", "aborted"}
    deadline = time.monotonic() + timeout_seconds
    state = "submitted"
    while time.monotonic() < deadline:
        state = await get_wu_state(wuid)
        if state in terminal:
            break
        await asyncio.sleep(2)
    return {"wuid": wuid, "state": state}

# ─── MCP Server ──────────────────────────────────────────────────────────────
app = Server("hpcc-cluster")


@app.list_tools()
async def list_tools():
    return [
        Tool(
            name="hpcc_list_workunits",
            description="List recent HPCC workunits (Thor/Roxie jobs). Returns job name, state, owner, duration, and cluster.",
            inputSchema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Number of workunits to return (default: 10)",
                        "default": 10,
                    },
                    "state": {
                        "type": "string",
                        "description": "Filter by state: completed, failed, running, blocked, submitted, compiled, aborted. Leave empty for all.",
                        "default": "",
                    },
                    "owner": {
                        "type": "string",
                        "description": "Filter by owner username. Leave empty for all.",
                        "default": "",
                    },
                },
            },
        ),
        Tool(
            name="hpcc_get_workunit",
            description="Get full details of a specific workunit including ECL source code, state, timings, exceptions, and results summary.",
            inputSchema={
                "type": "object",
                "properties": {
                    "wuid": {
                        "type": "string",
                        "description": "Workunit ID (e.g., W20260611-123456)",
                    },
                    "include_results": {
                        "type": "boolean",
                        "description": "Include output results (first 100 rows). Default: false.",
                        "default": False,
                    },
                },
                "required": ["wuid"],
            },
        ),
        Tool(
            name="hpcc_submit_ecl",
            description="Submit ECL code to the HPCC cluster for compilation and execution. Returns the new workunit ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "ecl_code": {
                        "type": "string",
                        "description": "ECL source code to compile and run",
                    },
                    "cluster": {
                        "type": "string",
                        "description": "Target cluster: 'thor' or 'roxie'. Default: 'thor'.",
                        "default": "thor",
                    },
                    "job_name": {
                        "type": "string",
                        "description": "Optional job name for the workunit",
                        "default": "",
                    },
                },
                "required": ["ecl_code"],
            },
        ),
        Tool(
            name="hpcc_run_ecl",
            description=(
                "Run ECL code end-to-end in a single call: submit it, wait for the job "
                "to finish, and return the output rows (or any compile/runtime errors). "
                "Use this for natural-language-to-ECL demos where the user wants the answer, "
                "not a workunit ID to poll. Best for quick queries that finish within ~60s."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ecl_code": {
                        "type": "string",
                        "description": "ECL source code to compile and run",
                    },
                    "cluster": {
                        "type": "string",
                        "description": "Target cluster: 'thor', 'hthor', or 'roxie'. Default: 'thor'.",
                        "default": "thor",
                    },
                    "job_name": {
                        "type": "string",
                        "description": "Optional job name for the workunit",
                        "default": "",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Max seconds to wait for completion (default: 60)",
                        "default": 60,
                    },
                },
                "required": ["ecl_code"],
            },
        ),
        Tool(
            name="hpcc_workunit_status",
            description="Check the current status of a workunit. Use to poll until completion.",
            inputSchema={
                "type": "object",
                "properties": {
                    "wuid": {
                        "type": "string",
                        "description": "Workunit ID to check",
                    },
                },
                "required": ["wuid"],
            },
        ),
        Tool(
            name="hpcc_get_results",
            description="Retrieve the output results of a completed workunit.",
            inputSchema={
                "type": "object",
                "properties": {
                    "wuid": {
                        "type": "string",
                        "description": "Workunit ID",
                    },
                    "sequence": {
                        "type": "integer",
                        "description": "Result sequence number (default: 0 for first result set)",
                        "default": 0,
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max rows to return (default: 100)",
                        "default": 100,
                    },
                },
                "required": ["wuid"],
            },
        ),
        Tool(
            name="hpcc_cluster_health",
            description="Get cluster health overview: active queues, running/queued jobs, node status, and throughput metrics.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="hpcc_topology",
            description="Get cluster topology: Thor/Roxie clusters, node counts, and machine details.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="hpcc_list_files",
            description="List logical files in the HPCC distributed file system (DFU). Shows file name, size, record count, and owner.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name_filter": {
                        "type": "string",
                        "description": "Filter files by name pattern (e.g., '::mydata::*'). Leave empty for all.",
                        "default": "",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max files to return (default: 20)",
                        "default": 20,
                    },
                },
            },
        ),
        Tool(
            name="hpcc_run_roxie_query",
            description="Execute a published Roxie query with parameters and return results.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query_name": {
                        "type": "string",
                        "description": "Name of the published Roxie query",
                    },
                    "params": {
                        "type": "object",
                        "description": "Query input parameters as key-value pairs",
                        "default": {},
                    },
                },
                "required": ["query_name"],
            },
        ),
        Tool(
            name="hpcc_describe_file",
            description=(
                "Describe a logical file in the DFU: its record structure (field names "
                "and types / ECL record definition), record count, and size. Use this "
                "before writing ECL against an existing dataset so you know its columns."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Logical file name (e.g. 'thor::mydata::people')",
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="hpcc_preview_file",
            description=(
                "Preview the first N rows of an existing logical file's actual data. "
                "Use together with hpcc_describe_file to ground ECL queries in real data."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Logical file name (e.g. 'thor::mydata::people')",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max rows to return (default: 50)",
                        "default": 50,
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="hpcc_syntax_check",
            description=(
                "Compile-only syntax check of ECL without running it on the cluster. "
                "Fast way to validate generated ECL and self-correct before a full submit. "
                "Returns any errors/warnings, or confirms the code is valid."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ecl_code": {
                        "type": "string",
                        "description": "ECL source code to syntax-check",
                    },
                    "cluster": {
                        "type": "string",
                        "description": "Target cluster used for the check. Default: 'thor'.",
                        "default": "thor",
                    },
                },
                "required": ["ecl_code"],
            },
        ),
        Tool(
            name="hpcc_list_queries",
            description=(
                "List published queries (e.g. Roxie queries) available on a query set, "
                "with their IDs and names. Use to discover what hpcc_run_roxie_query can run."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query_set": {
                        "type": "string",
                        "description": "Query set / target to list (e.g. 'roxie'). Default: 'roxie'.",
                        "default": "roxie",
                    },
                    "filter": {
                        "type": "string",
                        "description": "Optional name filter substring. Leave empty for all.",
                        "default": "",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max queries to return (default: 50)",
                        "default": 50,
                    },
                },
            },
        ),
        Tool(
            name="hpcc_abort_workunit",
            description="Abort a running or queued workunit. Use to stop a long-running or runaway job.",
            inputSchema={
                "type": "object",
                "properties": {
                    "wuid": {
                        "type": "string",
                        "description": "Workunit ID to abort",
                    },
                },
                "required": ["wuid"],
            },
        ),
        Tool(
            name="hpcc_delete_workunit",
            description="Delete a workunit and its results from the cluster. Useful for cleanup.",
            inputSchema={
                "type": "object",
                "properties": {
                    "wuid": {
                        "type": "string",
                        "description": "Workunit ID to delete",
                    },
                },
                "required": ["wuid"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "hpcc_list_workunits":
            params = {
                "PageSize": arguments.get("count", 10),
                "Sortby": "Date",
                "Descending": "true",
            }
            if arguments.get("state"):
                params["State"] = arguments["state"]
            if arguments.get("owner"):
                params["Owner"] = arguments["owner"]

            data = await esp_get("/WsWorkunits/WUQuery.json", params)
            workunits = as_list(data.get("WUQueryResponse", {}).get("Workunits", {}).get("ECLWorkunit"))

            results = []
            for wu in workunits:
                results.append({
                    "wuid": wu.get("Wuid"),
                    "jobname": wu.get("Jobname", ""),
                    "state": wu.get("State", ""),
                    "owner": wu.get("Owner", ""),
                    "cluster": wu.get("Cluster", ""),
                    "total_time": wu.get("TotalClusterTime", ""),
                })

            return [TextContent(type="text", text=json.dumps(results, indent=2))]

        elif name == "hpcc_get_workunit":
            wuid = arguments["wuid"]
            params = {
                "Wuid": wuid,
                "IncludeExceptions": "true",
                "IncludeSourceFiles": "true",
                "IncludeTimers": "true",
            }
            data = await esp_get("/WsWorkunits/WUInfo.json", params)
            wu = data.get("WUInfoResponse", {}).get("Workunit", {})

            result = {
                "wuid": wu.get("Wuid"),
                "jobname": wu.get("Jobname", ""),
                "state": wu.get("State", ""),
                "owner": wu.get("Owner", ""),
                "cluster": wu.get("Cluster", ""),
                "ecl_code": wu.get("Query", {}).get("Text", "") if isinstance(wu.get("Query"), dict) else wu.get("Query", ""),
                "total_time": wu.get("TotalClusterTime", ""),
                "exceptions": [],
                "timers": [],
            }

            # Extract exceptions
            for exc in as_list(wu.get("Exceptions", {}).get("ECLException"))[:5]:
                result["exceptions"].append({
                    "severity": exc.get("Severity", ""),
                    "message": exc.get("Message", ""),
                    "source": exc.get("Source", ""),
                })

            # Extract timers (top 5 by duration)
            for t in as_list(wu.get("Timers", {}).get("ECLTimer"))[:5]:
                result["timers"].append({
                    "name": t.get("Name", ""),
                    "value": t.get("Value", ""),
                })

            # Optionally include results
            if arguments.get("include_results"):
                try:
                    res_data = await esp_get("/WsWorkunits/WUResult.json", {
                        "Wuid": wuid,
                        "Sequence": 0,
                        "Count": 100,
                    })
                    result["results"] = res_data.get("WUResultResponse", {}).get("Result", "")
                except Exception:
                    result["results"] = "No results available"

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "hpcc_submit_ecl":
            ecl_code = arguments["ecl_code"]
            cluster = arguments.get("cluster", DEFAULT_CLUSTER)
            job_name = arguments.get("job_name", "")

            # Step 1: Create workunit
            create_resp = await esp_post("/WsWorkunits/WUCreate.json", {})
            wuid = create_resp.get("WUCreateResponse", {}).get("Workunit", {}).get("Wuid")

            if not wuid:
                return [TextContent(type="text", text="Error: Failed to create workunit")]

            # Step 2: Update with ECL code
            update_payload = {
                "Wuid": wuid,
                "QueryText": ecl_code,
            }
            if job_name:
                update_payload["Jobname"] = job_name
            await esp_post("/WsWorkunits/WUUpdate.json", update_payload)

            # Step 3: Submit
            await esp_post("/WsWorkunits/WUSubmit.json", {
                "Wuid": wuid,
                "Cluster": cluster,
            })

            return [TextContent(type="text", text=json.dumps({
                "status": "submitted",
                "wuid": wuid,
                "cluster": cluster,
                "job_name": job_name or "(unnamed)",
                "message": f"Workunit {wuid} submitted to {cluster}. Use hpcc_workunit_status to track progress.",
            }, indent=2))]

        elif name == "hpcc_run_ecl":
            ecl_code = arguments["ecl_code"]
            cluster = arguments.get("cluster", DEFAULT_CLUSTER)
            job_name = arguments.get("job_name", "")
            timeout_seconds = arguments.get("timeout_seconds", 60)

            # Create -> update with ECL -> submit, then poll to terminal/timeout
            run = await run_ecl_and_wait(ecl_code, cluster, timeout_seconds, job_name)
            wuid = run.get("wuid")
            if not wuid:
                return [TextContent(type="text", text="Error: Failed to create workunit")]
            state = run["state"]

            response = {"wuid": wuid, "cluster": cluster, "state": state}

            if state == "completed":
                response["results"] = await fetch_result_rows(wuid, sequence=0, count=100)
            elif state in {"failed", "aborted"}:
                response["errors"] = await fetch_wu_exceptions(wuid)
            else:
                response["note"] = (
                    f"Still '{state}' after {timeout_seconds}s. "
                    f"Use hpcc_workunit_status / hpcc_get_results with wuid {wuid}."
                )

            return [TextContent(type="text", text=json.dumps(response, indent=2))]

        elif name == "hpcc_workunit_status":
            wuid = arguments["wuid"]
            data = await esp_get("/WsWorkunits/WUQuery.json", {"Wuid": wuid})
            workunits = as_list(data.get("WUQueryResponse", {}).get("Workunits", {}).get("ECLWorkunit"))

            if not workunits:
                return [TextContent(type="text", text=f"Workunit {wuid} not found")]

            wu = workunits[0]
            return [TextContent(type="text", text=json.dumps({
                "wuid": wu.get("Wuid"),
                "state": wu.get("State", ""),
                "total_time": wu.get("TotalClusterTime", ""),
            }, indent=2))]

        elif name == "hpcc_get_results":
            wuid = arguments["wuid"]
            sequence = arguments.get("sequence", 0)
            count = arguments.get("count", 100)

            rows = await fetch_result_rows(wuid, sequence, count)
            return [TextContent(type="text", text=json.dumps({
                "wuid": wuid,
                "sequence": sequence,
                "row_count": len(rows),
                "truncated": len(rows) >= count,
                "rows": rows,
            }, indent=2))]

        elif name == "hpcc_cluster_health":
            # Activity (queues, running jobs)
            activity = await esp_get("/WsSMC/Activity.json")
            activity_data = activity.get("ActivityResponse", {})

            health = {
                "running_workunits": [],
                "queues": [],
                "summary": {},
            }

            # Extract running WUs
            for wu in as_list(activity_data.get("Running", {}).get("ActiveWorkunit")):
                health["running_workunits"].append({
                    "wuid": wu.get("Wuid"),
                    "owner": wu.get("Owner", ""),
                    "jobname": wu.get("Jobname", ""),
                    "state": wu.get("State", ""),
                    "duration": wu.get("Duration", ""),
                })

            # Extract queue info
            for q in as_list(activity_data.get("ThorClusterList", {}).get("TargetCluster")):
                health["queues"].append({
                    "name": q.get("ClusterName", ""),
                    "status": q.get("StatusDetails", ""),
                    "queue_status": q.get("QueueStatus", ""),
                })

            health["summary"] = {
                "running_count": len(health["running_workunits"]),
                "queue_count": len(health["queues"]),
            }

            return [TextContent(type="text", text=json.dumps(health, indent=2))]

        elif name == "hpcc_topology":
            # Containerized HPCC: the legacy TpClusterQuery/TpMachineQuery endpoints
            # return empty bodies. Cluster + node info comes from WsSMC/Activity,
            # and service/machine details from TpServiceQuery.
            data = await esp_get("/WsSMC/Activity.json")
            activity = data.get("ActivityResponse", {})

            result = {
                "build": activity.get("Build", ""),
                "clusters": [],
            }

            cluster_lists = {
                "thor": "ThorClusterList",
                "roxie": "RoxieClusterList",
                "hthor": "HThorClusterList",
            }
            for kind, key in cluster_lists.items():
                entries = (activity.get(key) or {}).get("TargetCluster", [])
                for c in (entries if isinstance(entries, list) else [entries]):
                    result["clusters"].append({
                        "name": c.get("ClusterName", ""),
                        "kind": kind,
                        "queue": c.get("QueueName", ""),
                        "queue_status": c.get("QueueStatus", ""),
                        "size": c.get("ClusterSize", ""),
                    })

            # Service processes (dfuserver, eclagent, etc.) with their machines
            try:
                svc = await esp_get("/WsTopology/TpServiceQuery.json", {"Type": "ALLSERVICES"})
                service_list = svc.get("TpServiceQueryResponse", {}).get("ServiceList", {}) or {}
                services = []
                for group_name, group in service_list.items():
                    if not isinstance(group, dict):
                        continue
                    for _svc_key, procs in group.items():
                        for p in (procs if isinstance(procs, list) else [procs]):
                            if not isinstance(p, dict):
                                continue
                            machines = (p.get("TpMachines", {}) or {}).get("TpMachine", [])
                            services.append({
                                "name": p.get("Name", ""),
                                "type": p.get("Type", ""),
                                "queue": p.get("Queue", ""),
                                "machines": [
                                    {"name": m.get("Name", ""), "address": m.get("Netaddress", "")}
                                    for m in (machines if isinstance(machines, list) else [machines])
                                    if isinstance(m, dict)
                                ],
                            })
                result["services"] = services
            except Exception as e:
                result["services"] = f"Unable to query services: {e}"

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "hpcc_list_files":
            params = {"PageSize": arguments.get("count", 20)}
            if arguments.get("name_filter"):
                params["LogicalName"] = arguments["name_filter"]

            data = await esp_get("/WsDfu/DFUQuery.json", params)
            files_data = as_list(data.get("DFUQueryResponse", {}).get("DFULogicalFiles", {}).get("DFULogicalFile"))

            files = []
            for f in files_data:
                files.append({
                    "name": f.get("Name", ""),
                    "owner": f.get("Owner", ""),
                    "size": f.get("IntSize", f.get("Totalsize", "")),
                    "records": f.get("RecordCount", ""),
                    "modified": f.get("Modified", ""),
                    "cluster": f.get("ClusterName", ""),
                })

            return [TextContent(type="text", text=json.dumps(files, indent=2))]

        elif name == "hpcc_run_roxie_query":
            query_name = arguments["query_name"]
            params = arguments.get("params", {})

            url = f"{WSECL_BASE_URL}/WsEcl/submit/query/roxie/{query_name}/json"
            resp = await client.post(url, json=params)
            resp.raise_for_status()
            data = resp.json()

            # Unwrap the "<QueryName>Response" envelope so the model gets clean rows.
            results = data
            if isinstance(data, dict):
                for key, val in data.items():
                    if key.endswith("Response") and isinstance(val, dict):
                        results = val.get("Results", val)
                        break

            return [TextContent(type="text", text=json.dumps({
                "query": query_name,
                "results": results,
            }, indent=2))]

        elif name == "hpcc_describe_file":
            logical_name = arguments["name"]
            data = await esp_get("/WsDfu/DFUInfo.json", {"Name": logical_name})
            fd = data.get("DFUInfoResponse", {}).get("FileDetail", {}) or {}

            ecl_record = fd.get("Ecl", "")
            # ESP's structured DataColumns is empty on containerized HPCC, so fall
            # back to parsing the ECL RECORD definition for field names/types.
            columns = []
            for col in as_list(fd.get("DataColumns", {}).get("DFUDataColumn")):
                columns.append({
                    "name": col.get("ColumnLabel", ""),
                    "type": col.get("ColumnType", ""),
                    "size": col.get("ColumnSize", ""),
                })
            if not columns:
                columns = parse_ecl_record(ecl_record)

            return [TextContent(type="text", text=json.dumps({
                "name": fd.get("Name", logical_name),
                "owner": fd.get("Owner", ""),
                "record_count": fd.get("RecordCount", ""),
                "size": fd.get("Filesize", ""),
                "content_type": fd.get("ContentType", ""),
                "cluster": fd.get("NodeGroup", fd.get("ClusterName", "")),
                "is_superfile": fd.get("isSuperfile", False),
                "ecl_record": ecl_record,
                "columns": columns,
            }, indent=2))]

        elif name == "hpcc_preview_file":
            logical_name = arguments["name"]
            count = arguments.get("count", 50)

            # DFUBrowseData cannot open files on containerized storage planes, so
            # read the data with a small generated ECL job using the file's own
            # record layout (from DFUInfo).
            info = await esp_get("/WsDfu/DFUInfo.json", {"Name": logical_name})
            fd = info.get("DFUInfoResponse", {}).get("FileDetail", {}) or {}
            ecl_record = fd.get("Ecl", "")
            content_type = (fd.get("ContentType", "") or "").lower()

            if fd.get("isSuperfile"):
                return [TextContent(type="text", text=json.dumps({
                    "name": logical_name,
                    "note": "Superfile — preview a subfile, or query it with hpcc_run_ecl.",
                }, indent=2))]
            if not ecl_record:
                return [TextContent(type="text", text=json.dumps({
                    "name": logical_name,
                    "note": "No ECL record layout available; cannot auto-preview. Use hpcc_run_ecl.",
                }, indent=2))]

            ref = logical_name if logical_name.startswith("~") else "~" + logical_name
            if content_type == "key":
                read_expr = f"INDEX(__Layout, '{ref}')"
            else:  # flat / csv / default
                read_expr = f"DATASET('{ref}', __Layout, THOR)"
            preview_ecl = (
                f"__Layout := {ecl_record}\n"
                f"__ds := {read_expr};\n"
                f"OUTPUT(CHOOSEN(__ds, {count}), NAMED('preview'));"
            )

            run = await run_ecl_and_wait(preview_ecl, DEFAULT_CLUSTER, 90,
                                         job_name=f"preview_{logical_name}")
            wuid = run.get("wuid")
            state = run.get("state")
            out = {"name": logical_name, "content_type": content_type,
                   "wuid": wuid, "state": state}
            if state == "completed":
                rows = await fetch_result_rows(wuid, sequence=0, count=count)
                out["row_count"] = len(rows)
                out["truncated"] = len(rows) >= count
                out["rows"] = rows
            elif state in {"failed", "aborted"}:
                out["errors"] = await fetch_wu_exceptions(wuid) if wuid else "no workunit"
            else:
                # Thor can be slow to warm up for file reads; hand back the WUID
                # so the caller can poll instead of treating this as a failure.
                out["note"] = (
                    f"Still '{state}'; Thor may be warming up. Use hpcc_get_results "
                    f"with wuid {wuid} shortly."
                )
            return [TextContent(type="text", text=json.dumps(out, indent=2))]

        elif name == "hpcc_syntax_check":
            ecl_code = arguments["ecl_code"]
            cluster = arguments.get("cluster", DEFAULT_CLUSTER)

            data = await esp_post("/WsWorkunits/WUSyntaxCheck.json", {
                "ECL": ecl_code,
                "Cluster": cluster,
            })
            resp = data.get("WUSyntaxCheckResponse", {}) or {}
            raw = [e for e in as_list(resp.get("Errors", {}).get("ECLException"))
                   if isinstance(e, dict)]

            def _spurious(e):
                # Containerized eclcc emits a bogus "File W....cc could not be
                # opened" (Code 2) when the check otherwise succeeds. Not an ECL error.
                return (str(e.get("Code", "")) == "2"
                        and "could not be opened" in str(e.get("Message", "")).lower())

            errors = [
                {"severity": e.get("Severity", ""), "message": e.get("Message", ""),
                 "code": e.get("Code", ""), "line": e.get("LineNo", ""),
                 "column": e.get("Column", "")}
                for e in raw if not _spurious(e)
            ]
            has_errors = any(str(e["severity"]).lower() == "error" for e in errors)

            return [TextContent(type="text", text=json.dumps({
                "valid": not has_errors,
                "errors": errors or "No errors or warnings.",
            }, indent=2))]

        elif name == "hpcc_list_queries":
            query_set = arguments.get("query_set", "roxie")
            params = {"QuerySetName": query_set, "PageSize": arguments.get("count", 50)}
            if arguments.get("filter"):
                params["QueryName"] = arguments["filter"]

            data = await esp_get("/WsWorkunits/WUListQueries.json", params)
            qs = data.get("WUListQueriesResponse", {}).get("QuerysetQueries", {})
            queries = []
            for q in as_list(qs.get("QuerySetQuery")):
                queries.append({
                    "id": q.get("Id", ""),
                    "name": q.get("Name", ""),
                    "wuid": q.get("Wuid", ""),
                    "suspended": q.get("Suspended", False),
                    "query_set": q.get("QuerySetId", query_set),
                })

            return [TextContent(type="text", text=json.dumps({
                "query_set": query_set,
                "count": len(queries),
                "queries": queries,
            }, indent=2))]

        elif name == "hpcc_abort_workunit":
            wuid = arguments["wuid"]
            await esp_post("/WsWorkunits/WUAbort.json", {"Wuids": wuid})
            state = await get_wu_state(wuid)
            return [TextContent(type="text", text=json.dumps({
                "wuid": wuid,
                "action": "abort",
                "state": state,
                "message": f"Abort requested for {wuid} (current state: {state}).",
            }, indent=2))]

        elif name == "hpcc_delete_workunit":
            wuid = arguments["wuid"]
            await esp_post("/WsWorkunits/WUDelete.json", {"Wuids": wuid})
            return [TextContent(type="text", text=json.dumps({
                "wuid": wuid,
                "action": "delete",
                "message": f"Workunit {wuid} deleted.",
            }, indent=2))]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except httpx.HTTPStatusError as e:
        return [TextContent(type="text", text=f"HTTP Error {e.response.status_code}: {e.response.text[:500]}")]
    except httpx.ConnectError:
        return [TextContent(type="text", text="Connection failed. Ensure port-forwards are running:\n  kubectl port-forward svc/eclwatch 8010:8010")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {type(e).__name__}: {str(e)}")]


# ─── Prompts ─────────────────────────────────────────────────────────────────
ECL_EXPERT_GUIDE = """You are an expert ECL (Enterprise Control Language) developer working \
against a live HPCC Systems cluster through this MCP server's tools. Translate the \
user's request into correct ECL, run it, and return the answer.

ECL essentials:
- ECL is declarative. Definitions are assignments using `:=` and end with `;`
  e.g.  evens := ds(n % 2 = 0);
- Emit every result you want returned with OUTPUT(...), optionally naming it:
  OUTPUT(COUNT(evens), NAMED('even_count'));
- Build an inline dataset with DATASET(count, TRANSFORM(reclayout, SELF.field := ...)).
  COUNTER is the 1-based row number inside the TRANSFORM.
  e.g.  ds := DATASET(100, TRANSFORM({UNSIGNED n}, SELF.n := COUNTER));
- Common built-ins: COUNT, SUM, AVE, MIN, MAX, SORT, DEDUP, TABLE, PROJECT, ITERATE,
  CHOOSEN(ds, n) to take the first n rows.
- String/number literals: 'single quotes' for strings; standard arithmetic operators.
- There is no `this` keyword; reference definitions by name.

Recommended workflow with the tools:
1. If the request involves an existing logical file, call `hpcc_describe_file` (and
   optionally `hpcc_preview_file`) first so you know the real field names and types.
2. Call `hpcc_syntax_check` on your ECL to catch compile errors cheaply, and fix any
   reported errors before running.
3. Call `hpcc_run_ecl` to execute and return the rows. Keep queries small so they
   finish within the default wait; for long jobs use `hpcc_submit_ecl` + status polling.
4. If a run fails, read the returned `errors` and self-correct, then retry.

Return the final answer clearly, citing the resulting rows."""


@app.list_prompts()
async def list_prompts():
    return [
        Prompt(
            name="ecl_expert",
            title="ECL Expert (NLP → ECL)",
            description=(
                "Turn the model into an ECL expert that translates natural language into "
                "correct ECL, validates it, runs it on the cluster, and returns the answer. "
                "Optionally pass a `task` to embed the request directly."
            ),
            arguments=[
                PromptArgument(
                    name="task",
                    description="The natural-language request to translate into ECL (optional).",
                    required=False,
                ),
            ],
        ),
    ]


@app.get_prompt()
async def get_prompt(name: str, arguments: dict | None = None):
    if name != "ecl_expert":
        raise ValueError(f"Unknown prompt: {name}")

    arguments = arguments or {}
    text = ECL_EXPERT_GUIDE
    task = arguments.get("task")
    if task:
        text += f"\n\n---\nUser request:\n{task}"

    return GetPromptResult(
        description="ECL expert guidance for natural-language-to-ECL on HPCC.",
        messages=[
            PromptMessage(role="user", content=TextContent(type="text", text=text)),
        ],
    )


# ─── Resources ───────────────────────────────────────────────────────────────
# Static catalog/topology resources plus a `hpcc://file/{logical_name}` template
# so a client can pull a dataset's schema in as context.
async def _resource_file_catalog() -> str:
    data = await esp_get("/WsDfu/DFUQuery.json", {"PageSize": 100})
    files_data = as_list(
        data.get("DFUQueryResponse", {}).get("DFULogicalFiles", {}).get("DFULogicalFile")
    )
    files = [
        {
            "name": f.get("Name", ""),
            "records": f.get("RecordCount", ""),
            "size": f.get("IntSize", f.get("Totalsize", "")),
            "uri": f"hpcc://file/{f.get('Name', '')}",
        }
        for f in files_data
    ]
    return json.dumps({"logical_files": files, "count": len(files)}, indent=2)


async def _resource_topology() -> str:
    data = await esp_get("/WsSMC/Activity.json")
    activity = data.get("ActivityResponse", {})
    clusters = []
    for kind, key in {"thor": "ThorClusterList", "roxie": "RoxieClusterList",
                      "hthor": "HThorClusterList"}.items():
        for c in as_list((activity.get(key) or {}).get("TargetCluster")):
            clusters.append({"name": c.get("ClusterName", ""), "kind": kind,
                             "queue_status": c.get("QueueStatus", "")})
    return json.dumps({"build": activity.get("Build", ""), "clusters": clusters}, indent=2)


async def _resource_file_schema(logical_name: str) -> str:
    data = await esp_get("/WsDfu/DFUInfo.json", {"Name": logical_name})
    fd = data.get("DFUInfoResponse", {}).get("FileDetail", {}) or {}
    columns = [
        {"name": col.get("ColumnLabel", ""), "type": col.get("ColumnType", "")}
        for col in as_list(fd.get("DataColumns", {}).get("DFUDataColumn"))
    ]
    return json.dumps({
        "name": fd.get("Name", logical_name),
        "record_count": fd.get("RecordCount", ""),
        "ecl_record": fd.get("Ecl", ""),
        "columns": columns,
    }, indent=2)


@app.list_resources()
async def list_resources():
    resources = [
        Resource(
            uri="hpcc://files",
            name="Logical file catalog",
            description="List of DFU logical files on the cluster (name, records, size).",
            mimeType="application/json",
        ),
        Resource(
            uri="hpcc://topology",
            name="Cluster topology",
            description="Thor/Roxie/hThor clusters and queue status.",
            mimeType="application/json",
        ),
    ]
    # Surface the first batch of logical files as concrete schema resources so
    # clients can browse them; the hpcc://file/{name} pattern works for any file.
    try:
        data = await esp_get("/WsDfu/DFUQuery.json", {"PageSize": 50})
        files_data = as_list(
            data.get("DFUQueryResponse", {}).get("DFULogicalFiles", {}).get("DFULogicalFile")
        )
        for f in files_data:
            fname = f.get("Name", "")
            if not fname:
                continue
            resources.append(Resource(
                uri=f"hpcc://file/{fname}",
                name=f"Schema: {fname}",
                description="Record structure / columns for this logical file.",
                mimeType="application/json",
            ))
    except Exception:
        pass  # Catalog browsing is best-effort; the template still resolves on read.
    return resources


@app.read_resource()
async def read_resource(uri):
    s = str(uri)
    rest = s[len("hpcc://"):] if s.startswith("hpcc://") else s
    rest = rest.rstrip("/")

    if rest == "files":
        return await _resource_file_catalog()
    if rest == "topology":
        return await _resource_topology()
    if rest.startswith("file/"):
        logical_name = rest[len("file/"):]
        return await _resource_file_schema(logical_name)

    raise ValueError(f"Unknown resource: {uri}")


# ─── Main ────────────────────────────────────────────────────────────────────
async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
