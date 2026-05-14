# 3. PG-aware pending vs ghost markers

**English** · [中文](03_pg_aware_pending.zh.md)

> The backfill drivers tracked progress by writing `*.posted` marker
> files alongside each ingested batch. Over time, the marker files
> drifted from the actual contents of PostgreSQL. Some batches were
> re-extracted 5 times; others were skipped forever. Marker drift is a
> classic CAP / cache-coherence problem dressed up in flat-file
> clothing.

## Symptom

A cron run reports *"95 batches pending"*. We extract them. PostgreSQL
gains zero new rows. Another cron run reports *"95 batches pending"*
again. Forever.

A different driver reports *"0 batches pending"*. PostgreSQL has the
data. We trust the driver and stop. Months later we run a coverage
audit and discover 1,799 batches in the input directory have never
made it to PG.

## The two drift modes

### Ghost markers

```
local *.posted file present  +  no corresponding row in PostgreSQL
```

How it happens: an early version of `streaming_post_v5.py` wrote the
marker first, then sent the HTTP POST. When the POST failed (e.g.
500 from the daemon under memory pressure), the marker stuck around.
The driver later thought the batch was done.

### PG-no-marker

```
row present in PostgreSQL  +  no local *.posted file
```

How it happens: a separate worker ran on the GPU server and POSTed
directly. Or the worker died after the POST succeeded but before the
marker was flushed to disk (filesystem buffer not synced). Or the
marker was on a network drive that became unmounted.

## Why local markers feel right but go wrong

Markers are appealing because:

1. They are cheap (a single `touch` per batch).
2. They survive across process restarts (which a Python dict would not).
3. They are inspectable with `ls`.

They go wrong because:

1. They are not in the same transaction as the database write.
2. They live on the *worker's* filesystem, not the database server's.
3. They can outlive the data they are supposed to represent.

## The fix: PG-aware pending

`memexa/core/pg_bid_cache.py` adds a 1-hour LRU over the authoritative
state in PostgreSQL.

```python
def list_pending_batches(source: str, all_input: list[Path]) -> list[Path]:
    posted_in_pg = pg_bid_cache.get(source)   # set of batch_id strings
    return [p for p in all_input if p.stem not in posted_in_pg]
```

The cache is refreshed when:

- The driver starts a new cron cycle.
- Any single `streaming_post_v5.post_card(...)` call succeeds (we
  add the new `batch_id` to the cache without a round-trip).
- 60 minutes have elapsed since last refresh.

## Migration: rebuilding markers from PG

The first time you turn on PG-aware pending, you will discover both
drift modes simultaneously. To bootstrap:

```bash
python -m src.tools.clean_ghost_markers --source wechat --dry-run
python -m src.tools.clean_ghost_markers --source wechat --apply
```

This script:

1. Loads all `*.posted` markers from the local driver state directory.
2. Loads all distinct `batch_id` values from PostgreSQL for that source.
3. **Ghost markers**: removed (they will be retried next cron cycle).
4. **PG-no-marker**: a placeholder marker is written so the driver
   stops re-extracting.

## Long-term hardening: POST-then-GET

`streaming_post_v5.py` now does this on every successful POST:

```python
resp = httpx.post(retain_url, json=card_payload, timeout=30)
resp.raise_for_status()
new_id = resp.json()["operation_id"]

# Verify the row actually exists before we write the marker
get_resp = httpx.get(f"{base_url}/memories/{new_id}", timeout=15)
if get_resp.status_code == 200:
    write_posted_marker(batch_id)
else:
    write_dead_marker(batch_id, reason=f"POST returned {new_id}, GET 404")
```

This eliminates ghost markers entirely, at the cost of one extra HTTP
round trip per card (~5 ms). Worth it.

## When this lesson generalises

Any time you track *completion* in a sidecar file (markers, lock
files, last-success timestamps) and the actual work product lives
somewhere else (database, S3, downstream service), expect drift. The
two cheap mitigations are:

1. Cache the authoritative state, do not own it.
2. Verify write-then-read before declaring success.

## See also

- `memexa/core/pg_bid_cache.py` — the LRU cache implementation.
- `memexa/extraction/streaming_post_v5.py` — POST-then-GET pattern.
- `memexa/drivers/backfill_v5_*_driver.py` — every driver consumes
  `pg_bid_cache.get()`.
