# 3. PG-aware pending vs ghost marker

[English](03_pg_aware_pending.md) · **中文**

> Backfill driver 在每个摄入 batch 旁写 `*.posted` marker 文件追踪进度。
> 时间长了, marker 和 PostgreSQL 实际内容漂移。一些 batch 被重抽 5 次;
> 另一些永远跳过。Marker 漂移是经典 CAP / cache-coherence 问题, 穿了
> 扁平文件外衣。

## 症状

一次 cron 报 *"95 batch pending"*。我们抽。PostgreSQL 0 新行。下次 cron
还报 *"95 batch pending"*。永远循环。

另一个 driver 报 *"0 batch pending"*。PostgreSQL 有数据。我们信 driver
停了。几个月后跑覆盖审计, 发现输入目录里 1799 个 batch 从未到 PG。

## 两种漂移模式

### Ghost marker

```
本地 *.posted 文件存在  +  PostgreSQL 没对应行
```

怎么来的: `streaming_post_v5.py` 早版本先写 marker, 再发 HTTP POST。
POST 失败 (比如 daemon 内存压力下返 500), marker 留着。Driver 之后以为
该 batch 完成了。

### PG-no-marker

```
PostgreSQL 有行  +  本地无 *.posted 文件
```

怎么来的: 另一个 worker 在 GPU server 上直接 POST 了。或 worker 在 POST
成功后 marker 还没刷盘就死了 (文件系统 buffer 没 sync)。或 marker 在
网络盘上被 unmount 了。

## 为啥本地 marker 感觉对但出错

Marker 有吸引力, 因为:

1. 便宜 (一次 `touch` per batch)
2. 跨进程重启活 (Python dict 做不到)
3. 用 `ls` 能查

它们出错, 因为:

1. 不在数据库写的同一事务里
2. 住 *worker* 的文件系统, 不是数据库 server 的
3. 能比它代表的数据活得更久

## 修法: PG-aware pending

`src/core/pg_bid_cache.py` 在 PostgreSQL 权威状态上加 1 小时 LRU。

```python
def list_pending_batches(source: str, all_input: list[Path]) -> list[Path]:
    posted_in_pg = pg_bid_cache.get(source)   # batch_id 字串集合
    return [p for p in all_input if p.stem not in posted_in_pg]
```

Cache 在以下时刻刷新:

- Driver 开始新 cron 循环
- 任何一次 `streaming_post_v5.post_card(...)` 成功 (新 `batch_id` 直接
  加进 cache 不再 round-trip)
- 上次刷新已过 60 分钟

## 迁移: 从 PG 重建 marker

第一次开 PG-aware pending, 你会同时发现两种漂移。引导:

```bash
python -m src.tools.clean_ghost_markers --source wechat --dry-run
python -m src.tools.clean_ghost_markers --source wechat --apply
```

脚本:

1. 从本地 driver state dir 载所有 `*.posted` marker
2. 从 PostgreSQL 载该 source 所有 distinct `batch_id` 值
3. **Ghost marker**: 删 (下次 cron 重试)
4. **PG-no-marker**: 写占位 marker 让 driver 停止重抽

## 长期硬化: POST-then-GET

`streaming_post_v5.py` 现在每次 POST 成功后:

```python
resp = httpx.post(retain_url, json=card_payload, timeout=30)
resp.raise_for_status()
new_id = resp.json()["operation_id"]

# 在写 marker 前验证 row 真的存在
get_resp = httpx.get(f"{base_url}/memories/{new_id}", timeout=15)
if get_resp.status_code == 200:
    write_posted_marker(batch_id)
else:
    write_dead_marker(batch_id, reason=f"POST returned {new_id}, GET 404")
```

完全消除 ghost marker, 代价是每张卡多一次 HTTP round trip (~5 ms)。值。

## 这个教训泛化时

只要你在 sidecar 文件 (marker, lock 文件, last-success 时间戳) 里追踪
*完成*, 而实际工作产品住别处 (数据库 / S3 / 下游服务), 都预期会漂移。
两个便宜缓解:

1. 缓存权威状态, 不要拥有
2. 声明成功前 verify write-then-read

## 相关

- `src/core/pg_bid_cache.py` — LRU 缓存实现
- `src/extraction/streaming_post_v5.py` — POST-then-GET 模式
- `src/drivers/backfill_v5_*_driver.py` — 每个 driver 消费 `pg_bid_cache.get()`
