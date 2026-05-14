# 1. Hindsight tags are OR, not AND

**English** · [中文](01_tags_are_or.zh.md)

> The hindsight `recall` API accepts a `tags=[...]` parameter. When the
> list has more than one element, every cursory reading of the API
> assumes the elements are AND-ed. They are not. They are OR-ed. The
> client must re-filter to recover AND semantics. Wall time of the bug
> from first symptom to fix: one weekend.

## Symptom

A new bank, `memory_full_v5`, had been populated with V2-envelope
cards. Every card carried `tags=["kind:event", "schema:v2",
"source:wechat"]` (or qq, email, etc.). A query that should have
returned ~20 WeChat events came back with 350 results from every
source.

```python
client.recall(
    bank="memory_full_v5",
    query="<keyword>",
    tags=["kind:event", "schema:v2", "source:wechat"],
)
# expected: ~20 wechat-only cards
# actual: 350 cards spanning every source
```

## Wrong intuition

The mental model was: *"tags is a refinement filter; each additional
tag narrows the set"*. This is how relational SQL `AND` works, this is
how Elasticsearch boolean queries work by default, and this is what the
docstring for `recall` seemed to imply.

## Actual behaviour

Hindsight's server-side `/recall` is built on top of a BM25 + cosine
hybrid retriever. `tags` is passed to the BM25 path as a *boost
disjunction*: any card with at least one matching tag gets a score
boost, but it is not filtered out. A card with no matching tag at all
can still be returned if its cosine similarity is high enough.

This is documented in `hindsight-api`'s source at
[`hindsight_api/retrieval/tag_boost.py`](https://github.com/vectorize-io/hindsight)
but is not in the public docstring of the Python client.

## Fix

Two-layer filter:

```python
results = client.recall(bank=bank, query=q, tags=["kind:event", "schema:v2"])
# Server-side recall is now permissive (OR-tagged).

# Client-side: enforce AND semantics
required = {"kind:event", "schema:v2", "source:wechat"}
results = [r for r in results if required.issubset(set(r.get("tags") or []))]
```

For high-traffic call sites we extracted this into
`_post_filter()` in `src/core/memory_query.py:107`.

## Knock-on consequence

The legacy `memory_full` bank predates V2 envelopes and has many cards
without any tags at all. If you pass *any* tag to `recall(...)` on
that bank, retrieval drops from 127 hits to 17. So the post-filter
function takes a `bank` argument and applies tag enforcement only for
banks where the schema is known to attach the expected tags. Legacy
banks get an empty `required` set.

```python
def _post_filter(results, *, bank: str, required: set[str]):
    if bank == "memory_full":
        return results              # legacy: do not enforce
    return [r for r in results if required.issubset(set(r.get("tags") or []))]
```

## Lesson

When a retrieval API says *"tags is a filter"*, ask which set operator
it uses before relying on it. The default for relational stores is
AND, the default for search engines (Lucene / OpenSearch / hybrid
recall pipelines) is OR. If you cannot read the server's source, write
a smoke test: insert two cards with tags `["a", "b"]` and `["a", "c"]`,
query with `tags=["b", "c"]`, count the results.

If you get 2 → OR. If you get 0 → AND. If you get 1 → something
weirder (probably tag-rank-then-cutoff).

## See also

- `src/core/memory_query.py:107` — `_post_filter` implementation
- `src/core/hindsight_client.py:205` — recall wrapper docstring
