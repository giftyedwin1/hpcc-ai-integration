# ECL Expert

## Overview

ECL Expert translates natural language requests into valid ECL (Enterprise Control Language), validates the generated code via syntax check, runs it on an HPCC cluster, and returns results — all in one flow. It also handles cluster operations like checking health, browsing files, managing workunits, and running published Roxie queries.

**Prerequisites**: Load the `user_mcp__hpcc_cluster` skill first to access all 16 HPCC tools:
`hpcc_cluster__hpcc_list_workunits`, `hpcc_cluster__hpcc_get_workunit`, `hpcc_cluster__hpcc_submit_ecl`, `hpcc_cluster__hpcc_run_ecl`, `hpcc_cluster__hpcc_workunit_status`, `hpcc_cluster__hpcc_get_results`, `hpcc_cluster__hpcc_cluster_health`, `hpcc_cluster__hpcc_topology`, `hpcc_cluster__hpcc_list_files`, `hpcc_cluster__hpcc_run_roxie_query`, `hpcc_cluster__hpcc_describe_file`, `hpcc_cluster__hpcc_preview_file`, `hpcc_cluster__hpcc_syntax_check`, `hpcc_cluster__hpcc_list_queries`, `hpcc_cluster__hpcc_abort_workunit`, `hpcc_cluster__hpcc_delete_workunit`.

## Workflow

### Step 1: Load HPCC Tools
- **Mode**: `deterministic`
- **Tool**: `load_skill("user_mcp__hpcc_cluster")`
- **Input**: None
- **Output**: All 16 HPCC cluster tools available
- **Validate**: Skill loads successfully with tools listed
- **On failure**: Advise user to check MCP connection in Settings → Capabilities → Connections

### Step 2: Classify the Request
- **Mode**: `agentic`
- **Input**: `{{query}}`
- **Output**: One of: `ecl_query` (need to generate & run ECL), `file_browse` (list/describe/preview DFU files), `cluster_ops` (health/topology/workunits), `roxie_query` (run published query)
- **Validate**: Classification maps to a clear next step
- **On failure**: Default to `ecl_query` if ambiguous; ask user if truly unclear

If the request is about cluster health, topology, or workunit management, skip to Step 7.
If the request is about browsing DFU files, skip to Step 6.
If the request is about running a published Roxie query, use `hpcc_cluster__hpcc_run_roxie_query`.

### Step 3: Discover Schema (if querying existing data)
- **Mode**: `agentic`
- **Tool**: `hpcc_cluster__hpcc_describe_file`, `hpcc_cluster__hpcc_preview_file`, `hpcc_cluster__hpcc_list_files`
- **Input**: File names mentioned or implied in `{{query}}`
- **Output**: Record structure (field names, types) and sample rows
- **Validate**: At least one file schema retrieved successfully
- **On failure**: If file not found, use `hpcc_cluster__hpcc_list_files` with a name filter to discover available files. Ask user to clarify if no match.

Understanding the schema before writing ECL prevents type mismatches and wrong field names. Always ground your ECL in the actual record definition.

### Step 4: Generate ECL Code
- **Mode**: `agentic`
- **Input**: `{{query}}` + schema from Step 3 (if applicable)
- **Output**: Valid ECL source code

ECL generation rules:
- Define RECORD types separately before using them in TABLE/DATASET (never inline `:=` assignments inside a record block)
- `dist` is a reserved word — never use it as a variable name
- For TABLE aggregations, use semicolons between fields in inline braces: `TABLE(ds, {field; UNSIGNED cnt := COUNT(GROUP)}, field)`
- Use OUTPUT with a NAMED clause so results are easy to identify
- Keep code concise — avoid unnecessary intermediate datasets
- For large-result queries, add a CHOOSEN or TOPN to limit output unless the user wants everything

### Step 5: Validate ECL
- **Mode**: `deterministic`
- **Tool**: `hpcc_cluster__hpcc_syntax_check`
- **Input**: Generated ECL code from Step 4, cluster = `{{cluster}}`
- **Output**: Confirmation of valid syntax, or error messages
- **Validate**: Response indicates no errors
- **On failure**: Read the error message, fix the ECL code, and re-check. Retry up to 3 times. Common fixes: reserved word conflicts, missing semicolons, type mismatches, undefined fields.

This step is critical — never skip it. Catching syntax errors before submission saves cluster resources and time.

### Step 6: Run ECL
- **Mode**: `deterministic`
- **Tool**: `hpcc_cluster__hpcc_run_ecl`
- **Input**: Validated ECL code, cluster = `{{cluster}}`, job_name derived from `{{query}}`
- **Output**: Result rows or error details
- **Validate**: Results returned successfully (state = completed)
- **On failure**: If timeout, use `hpcc_cluster__hpcc_submit_ecl` + `hpcc_cluster__hpcc_workunit_status` polling + `hpcc_cluster__hpcc_get_results` instead. If runtime error, diagnose from the error message and return to Step 4 to fix. If cluster issue, check health with Step 7.

Use `hpcc_run_ecl` for queries expected to finish within 60s. For longer jobs, submit and poll separately.

### Step 7: Cluster Operations (branch)
- **Mode**: `agentic`
- **Tool**: `hpcc_cluster__hpcc_cluster_health`, `hpcc_cluster__hpcc_topology`, `hpcc_cluster__hpcc_list_workunits`, `hpcc_cluster__hpcc_get_workunit`, `hpcc_cluster__hpcc_abort_workunit`, `hpcc_cluster__hpcc_delete_workunit`
- **Input**: `{{query}}`
- **Output**: Cluster status, workunit details, or confirmation of action
- **Validate**: Tool returns data without error
- **On failure**: If MCP connection error (coroutine/async issue), inform user to restart the session

### Step 8: Present Results
- **Mode**: `agentic`
- **Input**: Raw results from Steps 6 or 7
- **Output**: Formatted answer to the user's original `{{query}}`
- **Validate**: Answer addresses the user's intent, not just raw data dump
- **On failure**: If results are unclear, show both the interpretation and raw data

Format results as a markdown table when tabular. For scalar results, state them inline. Always show the ECL code that was run (in a code block) so the user can learn and iterate.

## Output

- The generated ECL code (in a fenced code block)
- Execution results formatted as a table or summary
- Workunit ID for reference
- Any warnings or notes about the query

## Lessons Learned

### Do
- Always syntax-check before running — it's fast and catches most issues
- Describe files before writing ECL against them — field names and types vary
- Use NAMED outputs so results are labeled clearly
- Pre-define RECORD types separately for TABLE operations
- Use `thor` cluster by default — it handles most workloads reliably
- Show the ECL code to the user so they can learn and refine

### Don't
- Don't use `dist` as a variable name (reserved keyword)
- Don't use inline `:=` inside RECORD type definitions
- Don't skip schema discovery — guessing field names leads to compile errors
- Don't use `hthor` for S3-backed DFS files (no S3 Express permissions)
- Don't run unbounded queries without CHOOSEN/TOPN — large results can timeout

### Common Failures
- **Syntax errors from reserved words**: ECL has many reserved words (`dist`, `count`, `group`). When used as identifiers, rename them (e.g., `score_dist`, `rec_count`)
- **Timeout on large queries**: Switch from `hpcc_run_ecl` to submit + poll + get_results pattern
- **File not found**: Use `hpcc_list_files` with partial name filter to discover correct logical file names
- **MCP coroutine errors**: Session-level issue; advise user to restart chat session
- **Type mismatch in TABLE**: Always check the source record definition and match types exactly
- **Tools not loaded**: Some HPCC tools may not be pre-loaded. If a tool call fails with "Tool X has not been loaded yet", call load_tools first then retry.

### When to Ask the User
- When the query is ambiguous about which dataset to use
- When multiple files match a name filter — ask which one
- When a query would produce very large output — confirm they want all rows
- When a destructive action is requested (abort/delete workunit) — confirm first
