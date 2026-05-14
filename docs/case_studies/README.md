# Case studies

**English** · [中文](README.zh.md)

> Reproducible methodology write-ups, distilled from real-world memexa
> deployments. Each case study answers "I have a memexa bank with my data
> in it — how do I turn it into something useful?"
>
> All examples use the synthetic [`demo_dataset/`](../../examples/demo_dataset/)
> personas (Alice / Bob / Carol / advisor@example.com). The methodology
> ports directly to your own data.

## What's in here

| # | Case study | Problem solved | Outputs |
|---|---|---|---|
| [01](01_lab_report_pipeline.md) | **Late-bound deliverable pipeline** | "I missed a deadline and need to assemble a deliverable fast." | LaTeX → PDF + action card |
| [02](02_meeting_brief_pattern.md) | **5-minute meeting brief** | "I see X tomorrow. What do I owe them? What's the open thread? What's the landmine?" | 4-section Markdown brief |

## What "case study" means here vs `lessons_learned/`

- `docs/lessons_learned/` — **engineering retrospectives**. Bugs we hit, fixes
  we shipped, debt we paid. Audience: contributors.
- `docs/case_studies/` (this dir) — **user-facing recipes**. Multi-command
  workflows that compose `memexa` subcommands into a deliverable. Audience:
  end users.

## How to write your own

If you have a workflow that's worth sharing:

1. Strip personal data ruthlessly — see [SECURITY.md](../../SECURITY.md).
   No real names, real emails, real phone numbers, real dates that
   correlate with specific people.
2. Substitute with `demo_dataset` personas where possible (Alice / Bob /
   Carol / `advisor@example.com`) so readers can run along.
3. Include the **command sequence verbatim** (copy-paste, not paraphrased).
4. Include **expected output structure** (not the raw text — just the
   shape: column headers, row count ranges).
5. End with "adapt to your own data" — one-liner substitution.

Open a PR. The bar is "would a different person executing your steps get
the same kind of output?" — not "is your writing polished".
