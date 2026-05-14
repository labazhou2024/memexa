# Hosted Extraction API (roadmap)

**English** · [中文](api_roadmap.zh.md)

> Status: **not implemented in v0.1.0.** This page exists so users know
> what's planned and can give early feedback. The OSS bundled prompt +
> BYO mode cover every use case until this lands.

## What it will be

A pay-per-use HTTP endpoint that runs a higher-quality extraction
prompt for you. You POST a batch, you get cards back. No subscription,
no dashboard, no monthly minimum — same shape as DeepSeek / OpenAI's
chat completions API.

```bash
curl https://api.memexa.io/v1/extract \
  -H "Authorization: Bearer mk_xxx" \
  -H "Content-Type: application/json" \
  -d '{
    "stage": "pass2",
    "source": "wechat",
    "batch": { ... }
  }'
```

## Pricing target

**1.2× the underlying provider's token rate.** No other charges.

Example with DeepSeek v4 flash as the underlying extractor:

| Item | Underlying | API rate |
|---|---|---|
| Input tokens  | $0.000028 / 1k | $0.0000336 / 1k |
| Output tokens | $0.000084 / 1k | $0.0001008 / 1k |

A typical batch (~3k in / 1.5k out) costs **~$0.0002**. A user that
ingests 1000 batches/month pays **~$0.20/month**.

## Why use this when basic mode is free

The bundled prompt is honest about its limits. It does NOT include:

- Source-aware tuning (the per-source heuristics the maintainer built
  over six months: wechat sender_name signal, qq temporary-session
  rule, email RFC2047 decode + spam filter, browser staying-time
  weighting, claude_code tool-result filter, audio 2-party speaker
  attribution)
- Identity manifest priority logic (resolution order across surface
  forms / pinyin initials / 5-message window / anaphora)
- Salience calibration tables (per-context numerical scoring)
- Time-resolution heuristics for Chinese fuzzy expressions

If you want those, you have three paths:

1. Write them yourself and use BYO mode (free).
2. Wait for the API endpoint (this page).
3. Run with the bundled prompt and accept lower accuracy.

## Data policy (when the API ships)

```
- Request / response data is NOT used for model training.
- Not sold or shared with third parties.
- Not used in marketing or demos.
- Caching: ≤ 30 day TTL, can be disabled per-request with
  X-Memexa-No-Retention: 1.
- DELETE /v1/data wipes everything tied to your key on demand.
```

Full policy text will live at `https://memexa.io/legal/data-policy`
when the endpoint deploys.

## How to be notified

This API will be announced on:

- [Memexa GitHub Releases](https://github.com/labazhou2024/memexa/releases)
- [Memexa Discussions](https://github.com/labazhou2024/memexa/discussions)

There is no email list and no marketing pipeline. The feature simply
ships when it ships.

## Why not bundle the production prompts in OSS instead

Honest answer: the maintainer wants to keep the option of charging for
extraction quality. Bundling the production prompts gives that away
permanently. Bundling a basic prompt + offering a paid endpoint
preserves the OSS contract (you can always run this locally for free)
while keeping a self-funded path for the project.

See [ROADMAP.md](../ROADMAP.md) for the rest of the v0.x plan.
