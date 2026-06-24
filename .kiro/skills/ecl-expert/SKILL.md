---
name: ecl-expert
description: Expert workflow for writing, validating, and running HPCC ECL via the @hpcc MCP tools, plus browsing DFU files, managing workunits, and running Roxie queries. Use whenever the user asks a data question to answer with ECL, wants ECL written or explained, wants to inspect an HPCC logical file, or needs HPCC cluster operations.
---

# ECL Expert

Translate natural-language requests into valid ECL, validate it with a syntax
check, run it on the HPCC cluster, and return results — in one flow. Also handle
cluster operations: health, topology, file browsing, workunit management, and
published Roxie queries.

**Tools**: provided by the `@hpcc` MCP server configured on the agent. All 16:
`@hpcc/hpcc_list_workunits`, `@hpcc/hpcc_get_workunit`, `@hpcc/hpcc_submit_ecl`,
`@hpcc/hpcc_run_ecl`, `@hpcc/hpcc_workunit_status`, `@hpcc/hpcc_get_results`,
`@hpcc/hpcc_cluster_health`, `@hpcc/hpcc_topology`, `@hpcc/hpcc_list_files`,
`@hpcc/hpcc_run_roxie_query`, `@hpcc/hpcc_describe_file`, `@hpcc/hpcc_preview_file`,
`@hpcc/hpcc_syntax_check`, `@hpcc/hpcc_list_queries`, `@hpcc/hpcc_abort_workunit`,
`@hpcc/hpcc_delete_workunit`. If a tool call fails with a connection error, tell
the user the cluster port-forwards may be down.

## Workflow

### Step 1: Classify the request
- **Mode**: agentic
- **Output**: one of `ecl_query` (generate & run ECL), `file_browse` (list/describe/preview DFU files), `cluster_ops` (health/topology/workunits), `roxie_query` (run a published query)
- If cluster health, topology, or workunit management → go to Step 5.
- If browsing DFU files → use the Step 2 tools and present.
- If running a published Roxie query → `@hpcc/hpcc_list_queries` then `@hpcc/hpcc_run_roxie_query`.
- If ambiguous, default to `ecl_query`; ask only if truly unclear.

### Step 2: Discover schema (when querying existing data)
- **Mode**: agentic
- **Tools**: `@hpcc/hpcc_describe_file`, `@hpcc/hpcc_preview_file`, `@hpcc/hpcc_list_files`
- Get the real record structure (field names, types) and sample rows before writing ECL.
- **On failure**: if the file isn't found, call `@hpcc/hpcc_list_files` with a name filter to discover it; ask the user to clarify if nothing matches.
- Never guess field names — ground the ECL in the actual record definition.

### Step 3: Generate ECL
- **Mode**: agentic
- Rules:
  - Define RECORD types separately before using them in TABLE/DATASET (no inline `:=` inside a record block).
  - `dist` is a reserved word — never use it as an identifier (rename to `score_dist`, etc.).
  - TABLE aggregations use semicolons between fields: `TABLE(ds, {field; UNSIGNED cnt := COUNT(GROUP)}, field)`.
  - Always `OUTPUT(expr, NAMED('name'))` so results are labeled.
  - Read a flat file: `DATASET('~name', Layout, THOR)`; an index: `INDEX(Layout, '~name')`.
  - Keep it concise; for large results add `CHOOSEN`/`TOPN` unless the user wants everything.

### Step 4: Validate, then run
- **Mode**: deterministic
- Call `@hpcc/hpcc_syntax_check`. If `valid: false`, read the errors (line/column/message), fix the ECL, and re-check. Retry up to 3 times. **Never skip this step.**
- Then call `@hpcc/hpcc_run_ecl` (use `cluster: thor` by default). Confirm `state: completed`.
- **On failure**: if it times out, switch to `@hpcc/hpcc_submit_ecl` + `@hpcc/hpcc_workunit_status` polling + `@hpcc/hpcc_get_results`. If it's a runtime error, diagnose from the error and return to Step 3.

### Step 5: Cluster operations (branch)
- **Mode**: agentic
- **Tools**: `@hpcc/hpcc_cluster_health`, `@hpcc/hpcc_topology`, `@hpcc/hpcc_list_workunits`, `@hpcc/hpcc_get_workunit`, `@hpcc/hpcc_abort_workunit`, `@hpcc/hpcc_delete_workunit`
- Confirm with the user before any destructive action (abort/delete).

### Step 6: Present results
- **Mode**: agentic
- Format tabular results as a markdown table; state scalar results inline.
- Always show the ECL that was run (in a code block) and the workunit ID.

## Lessons Learned

### Do
- Syntax-check before running — fast and catches most issues.
- Describe files before writing ECL against them — names and types vary.
- Use NAMED outputs; pre-define RECORD types separately for TABLE ops.
- Default to the `thor` cluster.
- Show the ECL so the user can learn and iterate.

### Don't
- Don't use `dist` as a variable name (reserved keyword).
- Don't use inline `:=` inside RECORD definitions.
- Don't skip schema discovery — guessing fields causes compile errors.
- Don't use `hthor` for S3-backed DFS files (no S3 Express permissions).
- Don't run unbounded queries without `CHOOSEN`/`TOPN`.

### Common failures
- **Reserved-word syntax errors**: rename identifiers like `dist`, `count`, `group`.
- **Timeout on large queries**: switch to submit + poll + get_results.
- **File not found**: use `@hpcc/hpcc_list_files` with a partial name filter.
- **Thor cold-start on preview**: `@hpcc/hpcc_preview_file` may return a non-completed
  state with a `wuid` and a "warming up" note — wait briefly and call
  `@hpcc/hpcc_get_results` with that `wuid`.
- **Type mismatch in TABLE**: match the source record definition exactly.

### When to ask the user
- Ambiguous about which dataset to use, or multiple files match a filter.
- A query would produce very large output — confirm they want all rows.
- A destructive action (abort/delete workunit) is requested.
