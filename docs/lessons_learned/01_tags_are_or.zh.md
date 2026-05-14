# 1. Hindsight tags 是 OR 不是 AND

[English](01_tags_are_or.md) · **中文**

> Hindsight `recall` API 接受 `tags=[...]` 参数。list 里有多个元素时,
> 任何粗读 API 的人都会假设这些元素是 AND。它们不是。它们是 OR。客户端
> 必须再过滤一遍恢复 AND 语义。Bug 从初次症状到修好: 一个周末。

## 症状

新 bank `memory_full_v5` 已经填上了 V2-envelope 卡。每张卡带
`tags=["kind:event", "schema:v2", "source:wechat"]` (或 qq, email 等)。
本该返回 ~20 张微信事件的查询返回了 350 张, 横跨所有 source。

```python
client.recall(
    bank="memory_full_v5",
    query="<keyword>",
    tags=["kind:event", "schema:v2", "source:wechat"],
)
# 期望: ~20 张 wechat-only 卡
# 实际: 350 张跨所有 source
```

## 错的直觉

心智模型是: *"tags 是 refinement 过滤; 每加一个 tag 缩窄结果集"*。
关系型 SQL `AND` 是这么干的, Elasticsearch boolean query 默认这么干的,
`recall` 的 docstring 看起来也是这意思。

## 实际行为

Hindsight 服务端 `/recall` 建在 BM25 + cosine 混合 retriever 上。`tags`
传给 BM25 路径作为 *boost disjunction*: 任何卡只要有一个匹配 tag 就拿
分数 boost, 但**不**被过滤掉。一张卡完全没匹配 tag 也能返回, 只要 cosine
相似度够高。

这在 `hindsight-api` 源码
[`hindsight_api/retrieval/tag_boost.py`](https://github.com/vectorize-io/hindsight)
里有记, 但 Python 客户端的公开 docstring 没说。

## 修法

两层过滤:

```python
results = client.recall(bank=bank, query=q, tags=["kind:event", "schema:v2"])
# 服务端 recall 现在是宽松的 (OR-tagged)。

# 客户端: 强制 AND 语义
required = {"kind:event", "schema:v2", "source:wechat"}
results = [r for r in results if required.issubset(set(r.get("tags") or []))]
```

高频调用点抽成了 `memexa/core/memory_query.py:107` 的 `_post_filter()`。

## 连带后果

Legacy `memory_full` bank 在 V2 envelope 之前就有了, 很多卡完全没 tag。
那个 bank 上调 `recall(...)` 传**任何** tag, 召回从 127 张掉到 17 张。
所以 post-filter 函数带 `bank` 参数, 只对 schema 已知会附预期 tag 的 bank
强制 tag。Legacy bank 用空 `required` 集合。

```python
def _post_filter(results, *, bank: str, required: set[str]):
    if bank == "memory_full":
        return results              # legacy: 不强制
    return [r for r in results if required.issubset(set(r.get("tags") or []))]
```

## 教训

retrieval API 说 *"tags is a filter"* 时, 在依赖它前先问它用哪个集合
operator。关系型存储默认是 AND, 搜索引擎 (Lucene / OpenSearch / 混合
recall pipeline) 默认是 OR。读不到 server 源码就写 smoke test: 插两张卡
tag `["a", "b"]` 和 `["a", "c"]`, 查 `tags=["b", "c"]`, 数结果。

返 2 → OR。返 0 → AND。返 1 → 更怪 (大概是 tag-rank-then-cutoff)。

## 相关

- `memexa/core/memory_query.py:107` — `_post_filter` 实现
- `memexa/core/hindsight_client.py:205` — recall wrapper docstring
