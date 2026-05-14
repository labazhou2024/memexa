# Hosted 抽取 API (路线图)

[English](api_roadmap.md) · **中文**

> 状态: **v0.1.0 未实现。** 这页存在是让用户知道在规划啥, 早期能给反馈。
> OSS bundled prompt + BYO 模式在它落地之前覆盖所有用例。

## 它会是什么

按用量付费的 HTTP endpoint, 用更高质量的抽取 prompt 帮你跑。你 POST 一个
batch, 拿到 card。无订阅, 无 dashboard, 无月度最低 — 形式跟 DeepSeek /
OpenAI chat completions API 一样。

```bash
curl https://api.memex.io/v1/extract \
  -H "Authorization: Bearer mk_xxx" \
  -H "Content-Type: application/json" \
  -d '{
    "stage": "pass2",
    "source": "wechat",
    "batch": { ... }
  }'
```

## 定价目标

**底层 provider token 费的 1.2 倍。** 别无收费。

按 DeepSeek v4 flash 当底层 extractor 举例:

| 项 | 底层 | API 费率 |
|---|---|---|
| Input tokens  | $0.000028 / 1k | $0.0000336 / 1k |
| Output tokens | $0.000084 / 1k | $0.0001008 / 1k |

典型 batch (~3k in / 1.5k out) 大约 **$0.0002**。一个月 1000 batch 的用户
付 **$0.20/月**。

## 基础模式免费, 为啥还用这个?

Bundled prompt 对自己的局限是诚实的。它**不**包括:

- Source-aware tuning (maintainer 6 个月调的 per-source heuristics:
  wechat sender_name 信号 / qq 临时会话规则 / email RFC2047 decode + spam
  过滤 / browser staying-time 加权 / claude_code tool-result 过滤 /
  audio 2-party 说话人归因)
- Identity manifest 优先级逻辑 (跨 surface form / pinyin 首字母 /
  5 消息窗口 / anaphora 的消解次序)
- Salience 校准表 (按上下文的数值评分)
- 中文模糊表达的时间消解 heuristics

要这些, 三条路:

1. 自己写, 用 BYO 模式 (免费)
2. 等 API endpoint (这页)
3. 用 bundled prompt 接受较低准确率

## 数据政策 (API 上线后)

```
- 请求 / 响应数据**不**用于训练模型
- 不卖, 不分享给第三方
- 不用于市场营销或 demo
- 缓存: ≤ 30 天 TTL, 单请求可加 X-Memex-No-Retention: 1 禁用
- DELETE /v1/data 按需擦除所有跟你 key 关联的数据
```

完整政策文本 endpoint 部署时会发布在
`https://memex.io/legal/data-policy`。

## 怎么得到通知

API 公布的地方:

- [Memex GitHub Releases](https://github.com/labazhou2024/memex/releases)
- [Memex Discussions](https://github.com/labazhou2024/memex/discussions)

没有邮件列表, 没有营销 pipeline。它什么时候发就什么时候发。

## 为啥不把 production prompt 直接打到 OSS?

诚实回答: maintainer 想保留按抽取质量收钱的可能。把 production prompt 打
进 OSS 就永久放弃了那个选择。打 basic prompt + 提供付费 endpoint, 既
守住 OSS 合同 (你永远能本地免费跑), 又留一条自筹路径给项目。

剩下 v0.x 计划见 [ROADMAP.zh.md](../ROADMAP.zh.md)。
