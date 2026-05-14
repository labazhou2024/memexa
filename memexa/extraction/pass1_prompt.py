"""Pass-1 IdentityAssertion extraction prompt for L0 v5 Stage 2.

Spec: docs/l0_v5/MASTER_PLAN.md §3.3 Stage 2 + §4.1

Pass-1 is a LIGHTWEIGHT, MANIFEST-FEEDING extraction pass. Goal:
- Identify self/other identity declarations ("我是 X / X 是 Y")
- Identify how-known assertions ("X 介绍我认识 Y")
- Identify abbreviation expansions ("wjc 是Carol")
- Flag candidate new entities (not in current manifest)
- DO NOT extract narrative / commitment / decision / etc.
  (those are Pass-2 territory)

Output is appended to assertion_queue.jsonl, processed by manifest_merge.py
in Stage 3 (规则 + LLM 仲裁合并) to grow the Identity Manifest.

Runs against your-org Gemma 4 31B AWQ (per CEO directive 2026-05-06).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional


PASS1_SYSTEM_PROMPT = """你是 IdentityAssertion 抽取器（轻量 pass）。

你的任务: 从一段聊天 batch 中**仅**抽取身份声明信号，反哺 Identity Manifest。
绝对不要做完整 narrative / commitment / decision 抽取——那是 Pass-2 任务。

【抽取目标】

类型 A: 自/他指身份声明 (IdentityAssertion)
  A.1 自指: "我是 X / 我叫 X / 大家叫我 X / 我就是 X"
       → assertion(speaker_wxid_hash, asserted_relation=is_self/is_aka, value=X)
  A.2 别名: "Y 也叫 X / Y 就是 X / X 别名 Y / X 即 Y"
       → assertion(asserted_relation=is_aka, value=X)
  A.3 拼音缩写: "wjc 是Carol / wjc(Carol) / 也就是 wjc"
       → assertion(asserted_relation=is_aka, value=缩写, expansion=全名)
  A.4 组织成员: "X 是 your-org 的 / X 在 your-org读书"
       → assertion(asserted_relation=is_in_org, value=组织名)
  A.5 拥有: "X 的 mac / 我的 primary-host"
       → assertion(asserted_relation=owns, value=物品名)
  A.6 协作关系: "X 是我的同学 / X 是我老板"
       → assertion(asserted_relation=works_with, value=X)
  A.7 公众人物提及: "X 是马斯克 / X 就是黄仁勋"
       → assertion(asserted_relation=is_public_figure, value=X)

类型 B: 认识方式 (RelationAssertion: how_known)
  B.1 介绍: "X 介绍我认识 Y / X 给我介绍了 Y / 通过 X 认识 Y"
       → relation(person_a=X, person_b=Y, type=introduced, context=场合)
  B.2 共同场合: "我和 Y 是在 Z 认识的 / 我和 Y 大学同学"
       → relation(person_a=self, person_b=Y, type=co_member/co_event, context=Z)

类型 C: 候选新实体 (NewEntityCandidate)
  - 出现的人名/缩写但不在已知 manifest 切片中
  - 仅记录 surface_form + 上下文片段, 不深度判断
  - 留给 Stage 3 合并算法 + 仲裁

【消解规则】

1. **manifest 切片优先**: 注入的 manifest_slice 是 ground truth, 命中即用 canonical_id
2. **拼音首字母匹配**: 如果 surface_form 是拼音首字母，且 manifest_slice.persons 内
   有人 pinyin_initials 命中 + 同 batch 上下文相符 → 立即合并
3. **同 batch 自指**: 找当前 batch sender_list, "我"指代发送者；"你"指被回复人
4. **不知道 → 留空**:
   - subject_canonical_id=null
   - resolution_confidence=ambiguous 或 unresolved
   - **绝不编造**

【绝对约束】

- **只抽身份信号**: 不抽 narrative / 不抽 commitment / 不抽 share / 不抽 question
- **只输出合规 JSON**: 见输出 schema
- **每条 assertion 必须有 quote** (原文片段, ≤80 字)
- **公众人物提及不进 RelationAssertion**: 比如"我们老板像马斯克"不算 introduced
- **不知道的 wxid_hash 用 sender 列表里实际存在的; 不要造假**
- **完成后输出 END_OF_OUTPUT 标记**

【输出 schema (JSON)】

```json
{
  "identity_assertions": [
    {
      "subject_wxid_hash": "<sender hash from sender_list>",
      "subject_canonical_id": "<canonical_id or null>",
      "asserted_relation": "is_self|is_aka|is_in_org|owns|works_with|introduced_by|met_at|is_public_figure",
      "asserted_value": "<value>",
      "expansion": "<optional, only for is_aka pinyin abbrev>",
      "quote": "<原文片段 ≤80 chars>",
      "confidence": "certain|inferred|ambiguous"
    }
  ],
  "relation_assertions": [
    {
      "person_a_canonical_id": "<canonical_id or null>",
      "person_a_surface": "<原文出现>",
      "person_b_canonical_id": "<canonical_id or null>",
      "person_b_surface": "<原文出现>",
      "relation_type": "introduced|co_member|co_event|kinship|romantic|professional|friendship",
      "context": "<场合自由文本>",
      "quote": "<原文片段 ≤80 chars>",
      "confidence": "certain|inferred|ambiguous"
    }
  ],
  "new_entity_candidates": [
    {
      "surface_form": "<表面形式>",
      "kind_hint": "person|organization|inanimate|public_figure|unknown",
      "context_snippet": "<上下文片段 ≤120 chars>",
      "co_occurring_known": ["<canonical_id 或 surface_form>", ...]
    }
  ]
}
END_OF_OUTPUT
```

【示例】

输入 batch:
```
sender_list:
  - wxid_hash=af0d4c08aa1037a05 (= Alice/Alice, is_self=true)
  - wxid_hash=1037a05c88f3a2bb (= Bob)

manifest_slice (相关切片):
  persons:
    person_alice:
      primary_name: Alice
      aka: [Alice, 粥粥]
      pinyin_initials: [hys]
      is_self: true
    person_maomao:
      primary_name: Bob
      aka: []
      is_self: false
  public_figures:
    pubfig_musk: {primary_name: Elon Musk, aka: [马斯克]}

messages:
  [2026-05-04 14:30] wxid_axxxx (af0d4c08aa1037a05): 我是Alice, 这周三去找你吃粥
  [2026-05-04 14:30] wxid_byyyy (1037a05c88f3a2bb): 上次老张介绍的那个 wjc 论文你看了吗
  [2026-05-04 14:31] wxid_axxxx (af0d4c08aa1037a05): 看了, Carol最近实验进度很猛
  [2026-05-04 14:31] wxid_byyyy (1037a05c88f3a2bb): 我们老板像马斯克一样疯
```

输出:
```json
{
  "identity_assertions": [
    {
      "subject_wxid_hash": "af0d4c08aa1037a05",
      "subject_canonical_id": "person_alice",
      "asserted_relation": "is_aka",
      "asserted_value": "Alice",
      "quote": "我是Alice",
      "confidence": "certain"
    },
    {
      "subject_wxid_hash": "1037a05c88f3a2bb",
      "subject_canonical_id": null,
      "asserted_relation": "is_aka",
      "asserted_value": "wjc",
      "expansion": "Carol",
      "quote": "上次老张介绍的那个 wjc 论文 / Carol最近实验进度很猛",
      "confidence": "inferred"
    }
  ],
  "relation_assertions": [
    {
      "person_a_canonical_id": null,
      "person_a_surface": "老张",
      "person_b_canonical_id": null,
      "person_b_surface": "wjc",
      "relation_type": "introduced",
      "context": "学术介绍 (论文)",
      "quote": "老张介绍的那个 wjc 论文",
      "confidence": "inferred"
    }
  ],
  "new_entity_candidates": [
    {
      "surface_form": "老张",
      "kind_hint": "person",
      "context_snippet": "上次老张介绍的那个 wjc 论文你看了吗",
      "co_occurring_known": ["wjc", "person_maomao"]
    },
    {
      "surface_form": "wjc",
      "kind_hint": "person",
      "context_snippet": "上次老张介绍的那个 wjc 论文 / Carol最近实验进度很猛",
      "co_occurring_known": ["老张", "person_alice"]
    }
  ]
}
END_OF_OUTPUT
```

注意:
- "我是Alice" → certain (manifest_slice 已确认)
- "wjc 是Carol" 通过下文"Carol最近实验进度"间接证实 → inferred (拼音匹配 + 时间近邻)
- "我们老板像马斯克" 不输出 RelationAssertion (是比喻, 不是介绍)
- "老张" 是新实体候选, 不在 slice 内 → 进 new_entity_candidates
- "马斯克" 是公众人物, 不输出新实体, 不输出 introduction
"""


def build_pass1_user_prompt(
    batch_id: str,
    chat_room: str,
    room_hash: str,
    batch_window_local: str,
    sender_list: List[Dict[str, Any]],
    manifest_slice: Dict[str, Any],
    messages: List[Dict[str, Any]],
) -> str:
    """Build the user-side prompt for one batch.

    Args:
        batch_id: batch identifier (= card.batch_id later)
        chat_room: original room display name
        room_hash: 32-char room hash
        batch_window_local: ISO time window 'start ~ end'
        sender_list: [{wxid_hash, alias_in_manifest_or_None, is_self}]
        manifest_slice: redacted manifest slice (per
            ManifestStore.extraction_slice_for_batch)
        messages: [{ts, wxid, wxid_hash, content}]

    Returns:
        Full user prompt str.
    """
    lines: List[str] = []
    lines.append(f"# 当前 batch")
    lines.append(f"batch_id: {batch_id}")
    lines.append(f"chat_room: {chat_room}")
    lines.append(f"room_hash: {room_hash}")
    lines.append(f"batch_window: {batch_window_local}")
    lines.append("")

    lines.append("# sender_list (本 batch 涉及的发送者, wxid_hash 已脱敏)")
    for s in sender_list:
        is_self_marker = "(is_self=true)" if s.get("is_self") else ""
        alias = s.get("alias_in_manifest_or_None", "manifest外")
        lines.append(f"  - wxid_hash={s['wxid_hash']} {is_self_marker} alias={alias}")
    lines.append("")

    lines.append("# manifest_slice (与本 batch 相关的已知实体, 仅供消解参考)")
    persons = manifest_slice.get("persons", {})
    if persons:
        lines.append("  persons:")
        for cid, p in persons.items():
            self_str = " is_self=true" if p.get("is_self") else ""
            lines.append(
                f"    {cid}: primary={p['primary_name']!r}, "
                f"aka={p['aka']}, pinyin={p['pinyin_initials']}{self_str}"
            )
    orgs = manifest_slice.get("organizations", {})
    if orgs:
        lines.append("  organizations:")
        for cid, o in orgs.items():
            lines.append(
                f"    {cid}: primary={o['primary_name']!r}, aka={o['aka']}"
            )
    inanimate = manifest_slice.get("inanimate", {})
    if inanimate:
        lines.append("  inanimate:")
        for cid, it in inanimate.items():
            lines.append(
                f"    {cid}: primary={it['primary_name']!r}, "
                f"aka={it['aka']}, owned_by={it.get('owned_by')}"
            )
    pf = manifest_slice.get("public_figures", {})
    if pf:
        lines.append("  public_figures:")
        for cid, p in pf.items():
            lines.append(
                f"    {cid}: primary={p['primary_name']!r}, aka={p['aka']}"
            )
    lines.append("")

    lines.append("# messages (按时间序)")
    for m in messages:
        ts = m.get("ts", "??")
        wh = m.get("wxid_hash", "??")
        content = (m.get("content") or "").strip()
        if len(content) > 500:
            content = content[:497] + "..."
        lines.append(f"[{ts}] wxid_hash={wh}: {content}")
    lines.append("")

    lines.append("# 任务: 仅输出合规 JSON + END_OF_OUTPUT")
    return "\n".join(lines)


def compute_pass1_prompt_sha(system_prompt: str, user_prompt: str) -> str:
    """Stable hash of the full prompt (system + user) for audit."""
    h = hashlib.sha256()
    h.update(system_prompt.encode("utf-8"))
    h.update(b"\x00")
    h.update(user_prompt.encode("utf-8"))
    return h.hexdigest()[:32]


# ────────────────────────── Output validation ──────────────────────────

VALID_ASSERTION_RELATIONS = {
    "is_self", "is_aka", "is_in_org", "owns", "works_with",
    "introduced_by", "met_at", "is_public_figure",
}
VALID_RELATION_TYPES = {
    "introduced", "co_member", "co_event", "kinship",
    "romantic", "professional", "friendship",
}


class Pass1OutputError(ValueError):
    """Raised when LLM output doesn't conform to expected schema."""


def parse_pass1_output(raw: str) -> Dict[str, List[Dict[str, Any]]]:
    """Parse and validate LLM output.

    Returns dict with keys: identity_assertions, relation_assertions, new_entity_candidates.
    Raises Pass1OutputError on malformed output.
    """
    if not raw or not isinstance(raw, str):
        raise Pass1OutputError("empty or non-string output")

    # Strip the END_OF_OUTPUT marker
    text = raw.split("END_OF_OUTPUT")[0].strip()

    # Extract JSON block (handle ```json ... ``` wrappers)
    if "```" in text:
        # Find the JSON block
        idx_start = text.find("```json")
        if idx_start < 0:
            idx_start = text.find("```")
        if idx_start >= 0:
            idx_start = text.find("\n", idx_start) + 1
            idx_end = text.find("```", idx_start)
            if idx_end > 0:
                text = text[idx_start:idx_end].strip()

    # Locate JSON object
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace < 0 or last_brace < first_brace:
        raise Pass1OutputError("no JSON object found in output")
    text = text[first_brace:last_brace + 1]

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise Pass1OutputError(f"JSON parse failed: {e}") from e

    if not isinstance(obj, dict):
        raise Pass1OutputError("output root must be object")

    # Validate identity_assertions
    ias = obj.get("identity_assertions", [])
    if not isinstance(ias, list):
        raise Pass1OutputError("identity_assertions must be list")
    for i, a in enumerate(ias):
        if not isinstance(a, dict):
            raise Pass1OutputError(f"identity_assertion[{i}] not dict")
        for k in ("subject_wxid_hash", "asserted_relation",
                  "asserted_value", "quote", "confidence"):
            if k not in a:
                raise Pass1OutputError(f"identity_assertion[{i}] missing {k!r}")
        if a["asserted_relation"] not in VALID_ASSERTION_RELATIONS:
            raise Pass1OutputError(
                f"identity_assertion[{i}] bad asserted_relation "
                f"{a['asserted_relation']!r}"
            )
        if a["confidence"] not in {"certain", "inferred", "ambiguous"}:
            raise Pass1OutputError(
                f"identity_assertion[{i}] bad confidence {a['confidence']!r}"
            )
        if not a["quote"]:
            raise Pass1OutputError(f"identity_assertion[{i}] empty quote")

    # Validate relation_assertions
    ras = obj.get("relation_assertions", [])
    if not isinstance(ras, list):
        raise Pass1OutputError("relation_assertions must be list")
    for i, r in enumerate(ras):
        if not isinstance(r, dict):
            raise Pass1OutputError(f"relation_assertion[{i}] not dict")
        if r.get("relation_type") not in VALID_RELATION_TYPES:
            raise Pass1OutputError(
                f"relation_assertion[{i}] bad relation_type "
                f"{r.get('relation_type')!r}"
            )

    # Validate new_entity_candidates
    necs = obj.get("new_entity_candidates", [])
    if not isinstance(necs, list):
        raise Pass1OutputError("new_entity_candidates must be list")

    return {
        "identity_assertions": ias,
        "relation_assertions": ras,
        "new_entity_candidates": necs,
    }


__all__ = [
    "PASS1_SYSTEM_PROMPT",
    "build_pass1_user_prompt",
    "compute_pass1_prompt_sha",
    "parse_pass1_output",
    "Pass1OutputError",
]
