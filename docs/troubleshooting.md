# Troubleshooting

**English** · [中文](troubleshooting.zh.md)

When something is wrong, walk the layered diagnosis instead of guessing.
Each section below is one common failure mode with the actual exit
criterion that proves it fixed.

## Layer 0 — Is the install sane?

```bash
memexa version            # prints memexa + python + dep versions
memexa config             # prints every resolved env + file path
memexa doctor             # round-trips backend + LLM + identity
```

If `memexa doctor` shows all `[ok]`, the install is sane and the problem
is in your data path. Skip to "Layer 2".

## Layer 1 — Backend won't come up

### Symptom: `curl http://127.0.0.1:8888/healthz` connection refused

```bash
docker compose -f docker-compose.example.yml logs hindsight | tail -30
```

Look for these lines:

| Log line                                           | Cause                | Fix                                              |
|----------------------------------------------------|----------------------|--------------------------------------------------|
| `OperationalError: could not connect to server`    | Postgres not ready   | `docker compose ps`; wait for `pg` healthcheck   |
| `psycopg2.errors.UndefinedFile: vector.so`         | pgvector missing     | Use the bundled image; or `CREATE EXTENSION vector;` |
| `ImportError: No module named hindsight`           | Old image tag        | `docker compose pull`                            |
| `RuntimeError: tiktoken failed to download`        | No internet, no cache| Pre-download: `python -c "import tiktoken;tiktoken.encoding_for_model('gpt-4')"` |
| `[Errno 98] Address already in use`                | Port 8888 taken      | `lsof -i :8888` then kill or change port mapping |

Exit criterion: `curl -sf http://127.0.0.1:8888/healthz` returns 200.

### Symptom: backend up but `bank does not exist`

The bank is created on first retain, not on container start. Run the
demo ingest to materialize it:

```bash
make demo-ingest
curl -s http://127.0.0.1:8888/v1/default/banks/memory_full_v5/stats
```

## Layer 2 — Ingestion silently produces no cards

### Diagnosis order

1. Does the builder write batch files?
   ```bash
   ls data/l0_v5/input_batches/<YYYY-MM-DD>/ | wc -l
   ```
   Zero → the builder rejected your raw export. Run with `--verbose` to
   see the parsing decisions.

2. Does Stage A produce verdicts?
   ```bash
   ls data/l0_v5/work/cards_v2_*/qwen.jsonl 2>/dev/null | head
   ```
   Empty → Stage A LLM is failing. Check `memexa doctor` LLM gate probe.

3. Does Stage B produce cards?
   ```bash
   ls data/l0_v5/work/cards_v2_*/27b.jsonl 2>/dev/null | head
   ```
   Empty → Stage B LLM is malforming JSON. Switch to a larger model or
   add `/no_think` directive (Qwen3 family — see
   [lessons_learned/05_qwen3_no_think.md](lessons_learned/05_qwen3_no_think.md)).

4. Does Stage D POST succeed?
   ```bash
   ls data/l0_v5/work/posted_v5_*/*.posted 2>/dev/null | wc -l
   ls data/l0_v5/work/posted_v5_*/*.dead   2>/dev/null | wc -l
   ```
   Many `.dead`, few `.posted` → run the recovery tool:
   `python -m tools.recover_phaseB_dead_letter`.

### Symptom: cron orchestrator exits but driver `n_runs=0`

Most likely the driver crashed inside the subprocess. Logs:

```bash
ls -la data/maintenance_logs/ | tail
tail -100 data/maintenance_logs/memexa_core_cron_orchestrator_*.log
```

Look for the per-driver `dispatch '<id>' ...` line. The next line tells
you exit code + duration. A duration > 240 s usually means the LLM
provider hung; see "LLM provider timeout" below.

### Symptom: driver re-extracts the same batches every cron run

Marker / PG truth drift. Force-rebuild the cache from PG:

```bash
python -m src.core.pg_bid_cache all --force-refresh
```

Exit criterion: next cron run shows `pending=0` for the affected source.

## Layer 3 — LLM provider issues

### Symptom: `memexa doctor` LLM probe returns 401 / 403

API key wrong or expired. `memexa config` to inspect the masked value;
re-issue if needed.

### Symptom: probe returns 200 but cron silently produces 0 cards

The provider is responding but returning malformed JSON. Capture one
real call:

```bash
MEMEXA_HINDSIGHT_TRACE_LOG=/tmp/memexa_trace.jsonl \
  python -m src.drivers.backfill_v5_wechat_driver --once --verbose
jq . /tmp/memexa_trace.jsonl
```

Common pathologies:

- Empty `choices[].message.content` → model context window overflow;
  reduce batch size.
- Thinking-only output (Qwen3 family) → append `/no_think` directive.
- JSON wrapped in markdown fences → already handled by the parser, but
  if you see this in trace, the parser threshold may have shifted.
- Hallucinated tool-call frames → the extractor model is fine-tuned for
  function calling and ignores the JSON-only system prompt. Switch model.

### Symptom: LLM provider timeout after 60 s

The default `httpx` timeout is short for chat-completions with 30 B+
models. The driver already passes a 4-hour timeout; the issue is usually
that the provider itself dropped the connection. Check the provider's
own logs.

For vLLM specifically:

- GPU OOM → reduce `--max-model-len` or batch size
- Engine deadlock under high concurrency → cap `MEMEXA_EXTRACT_CONCURRENT=5`
  (sweet spot for most vLLM deploys)

## Layer 4 — Query returns nothing useful

See [usage_guide.md#troubleshooting](usage_guide.md#troubleshooting) for
the Q1/Q2/Q3 diagnostic ladder.

Common pitfalls:

- Searching a person with `topic` instead of `arc` (see hard rule).
- Default `--salience 0.3` filtering out everything low-priority. Pass
  `--salience 0.0` to see the full distribution.
- Querying the legacy `memory_full` bank by accident. Default is `_v5`;
  unset `MEMEXA_HINDSIGHT_BANK` if you've shadowed it.
- Windows GBK terminal mangling Chinese output. Set
  `PYTHONIOENCODING=utf-8` or use Windows Terminal.

## Layer 5 — Cron orchestrator wider issues

### Symptom: GraphMaintenance schtask fails with rc=0x00041306

Per-cycle TIMEOUT. The Windows Job Object kills the orchestrator + all
children when it exceeds the schtask `ExecutionTimeLimit`. Common cause:
audio driver runs inside the orchestrator and its ASR step takes 50 min.

Fix: move audio out of the orchestrator (already done in the manifest;
set `skip_in_orchestrator: true` if you re-add it). Audio gets its own
6-hour schtask.

### Symptom: dashboard "Cron health" shows mismatched count vs `Get-ScheduledTask`

`schtask_health.py` checks task age in addition to last-result RC; the
dashboard only looks at RC. A task that ran 8 hours ago with RC=0 is
"healthy" to the dashboard, "stale" to `schtask_health`. Both are
correct from their respective viewpoints — pick the right one for your
question.

## Layer 6 — Dashboard

### Symptom: dashboard renders but every panel says "—"

Localhost-only install. Set `MEMEXA_DASHBOARD_HOSTS` env to a JSON array
to light up remote-host panels. See
[`src/dashboard/sys_monitor/server.py`](../src/dashboard/sys_monitor/server.py)
docstring for the schema.

### Symptom: "Graph queries" panel is empty even after running queries

Check the log path:

```bash
python -c "from src.core.memory_query import _QUERY_LOG_PATH; print(_QUERY_LOG_PATH)"
ls -la $(python -c "from src.core.memory_query import _QUERY_LOG_PATH; print(_QUERY_LOG_PATH)")
```

If the path is under `_MEI*\Temp\` you have a PyInstaller frozen-path
issue — re-run from source, not the bundled exe. The path resolver in
`src/core/_path_resolver.py` is meant to defeat this, so file an issue
with the exact path printed.

## Last resort

Open an issue with:

1. Output of `memexa version` + `memexa config` + `memexa doctor`.
2. The exact command that fails + full traceback.
3. The relevant cron log slice (`data/maintenance_logs/*.log`).

Maintainer triage is best-effort; clear repro saves a round-trip.
