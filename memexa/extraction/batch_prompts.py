"""Layer C — type-specific batch extraction prompts.

5 prompts + INFORMATIVE fallback. All prompts now (CEO 2026-05-05 quality fix):
  - subject MUST be a sender_display_name from the batch (NOT chat_room name, NOT message-text noun)
  - object MUST NOT be the chat_room_display_name
  - object MUST NOT be raw message text (must be a noun-entity: person/place/event/topic/time)
  - LLM should produce multiple facts when warranted (no hard 1-fact cap on chitchat)

Caller wraps these as: f"<TYPE_PROMPT>\n\n对话数据:\n{batch_json}\n\nJSON:"
"""

# Common entity-class constraint header (CEO 2026-05-05 quality fix)
# Inserted into every prompt to enforce subject/object discipline.
_ENTITY_RULES = (
    "**实体规则 (硬约束, 任何违反则丢弃该 fact)**:\n"
    "  1. subject 必须是 batch 中的某个 sender 显示名 (从 messages[].sender 中选)。\n"
    "     不可用群名 (chat_room) / 消息内容 / 名词性短语 (如'活动'/'腾讯会议'/'习题课') 当 subject。\n"
    "  2. object 不能等于 chat_room 名 (即对话发生地, payload.chat_room 字段)。\n"
    "  3. object 不能是消息原文片段 (如'抱抱抱抱' / '下次不这么晚了 对不起')；\n"
    "     必须是 *实体* (人名/地点/事件/话题/时间/链接/资源)。\n"
    "  4. (s, p, o) 在同 batch 内不重复; 不要为同一动作产 3 个谓词 fan-out (如 shared_link + shared_resource + recommended 同一对象)。\n\n"
)


PROMPT_CHITCHAT = (
    "/no_think\n"
    "这段对话以 **闲聊/亲昵互动** 为主 (chitchat)。\n"
    + _ENTITY_RULES +
    "**至少输出 1 条聚合互动 fact** + 任何具体事实:\n"
    "  - 必有: 互动关系 fact, predicate ∈ {intimate_chat_with, small_talk_with}, "
    "subject = 主要 sender, object = 对方 sender 显示名 (不是群名/不是消息内容/不是表情)\n"
    "  - 加: 闲聊里的具体事实 (人/地点/时间/活动/计划/情绪/偏好/决定/承诺), "
    "predicate: mentioned_topic / scheduled_for / shared_resource / agreed_with / "
    "committed_to / recommended / interested_in / dislikes / takes_position / asked_pending\n"
    "  - 每条 fact 携带原始 ts (从对应 msg)\n\n"
    "**绝不输出 []**。即使是纯亲昵问候，也要输出 1 条 small_talk_with 互动 fact "
    "(subject=主要发言人, object=对方 sender 名)。"
)


PROMPT_PLANNING = (
    "/no_think\n"
    "这段对话含 **计划/约定/承诺** (planning)。\n"
    + _ENTITY_RULES +
    "提取所有 actionable fact:\n"
    "  - predicate ∈ {plans_with, scheduled_for, committed_to, agreed_with}\n"
    "  - subject: 发起人的 sender 显示名 (不是会议名/活动名)\n"
    "  - object: 计划对象 (具体人/事件/时间/地点)\n"
    "  - 每条 fact 必须可执行 (有时间/对象/动作)\n"
    "  - ts: 该 fact 出现的具体 msg 时间\n\n"
    "示例: (Alice, plans_with, Bob) + (Alice, scheduled_for, 明早9点)。\n\n"
    "上限 5 条 fact (避免冗长); 无明确计划输出 []."
)


PROMPT_SHARING = (
    "/no_think\n"
    "这段对话以 **分享内容** 为主 (链接/图片/话题/资源)。\n"
    + _ENTITY_RULES +
    "提取分享的核心内容 fact:\n"
    "  - predicate ∈ {shared_link, mentioned_topic, recommended, shared_resource}\n"
    "  - **每个被分享对象只用 1 个 predicate** (绝不 fan-out 多 predicates)。链接 → shared_link, 资源/视频/图 → shared_resource, 推荐性表述 → recommended, 单纯提及 → mentioned_topic\n"
    "  - subject: 分享者 sender 名 (不是 chat_room/账号 ID)\n"
    "  - object: 被分享内容的简短标题 (1-30 字, 不是 url, 不是消息原文)\n"
    "  - 同 (subject, object) 不重复;  不同发分享者各取一条\n\n"
    "上限 3 条 fact (每条独立 link/topic); 无分享内容输出 []."
)


PROMPT_ARGUMENT = (
    "/no_think\n"
    "这段对话含 **观点分歧/讨论/争论** (argument)。\n"
    + _ENTITY_RULES +
    "提取核心立场 fact:\n"
    "  - predicate ∈ {disagrees_with, takes_position, advocates, questions}\n"
    "  - subject: 发言人 sender 名\n"
    "  - object: 立场/观点摘要 (1-50 字, 不是逐字消息)\n"
    "  - 双方各取 1-2 条核心立场, 不要逐句提\n\n"
    "上限 4 条 fact (双方各 2); 无明确分歧输出 []."
)


PROMPT_UNRESOLVED = (
    "/no_think\n"
    "这段对话被分类为 **未回应的 1v1 短问询** (unresolved)。\n"
    + _ENTITY_RULES +
    "**严格输出门槛 (任一不满足就 输出 [])**:\n"
    "  1. 必须**真的是问询**: msg 含 ？ OR ? OR 问询动词 (问/请教/咨询/求/能否/是否/有没有)。\n"
    "  2. 不能是**情感/亲昵/问候**: 抱抱/想你/早安/晚安/对不起/睡了 → 输出 []。\n"
    "  3. 不能是**单字/表情/单词** (抱抱/嗯嗯/亲昵称呼/无意义象声词) → 输出 []。\n\n"
    "若通过门槛, **输出至多 1 条 fact**:\n"
    "  - subject: 问询者 sender 名 (不是 chat_room_name)\n"
    "  - predicate: `asked_pending`\n"
    "  - object: 问询的核心实体/事 (≤30 字, 必须抽象出问询的对象/动作; 不是消息原文)\n"
    "  - 例: msg='Alice 老师在办公室, 找她咨询论文?' → object='Alice 论文咨询'\n"
    "  - tentative=true (caller 处理)"
)


PROMPT_INFORMATIVE = (
    "/no_think\n"
    "分析这段对话, 提取真正有价值的事实。\n"
    + _ENTITY_RULES +
    "每个事实必须:\n"
    "  - subject: sender 显示名 (不是群名/消息内容)\n"
    "  - predicate: 具体语义动词 (动词或关系)\n"
    "  - object: 实体 (人/事/物/时间/地点); 不能是消息原文\n"
    "  - sender: 该 fact 的发言人\n"
    "  - ts: 该 fact 出现的时间戳 (ISO 格式, 从对应 msg 的 ts)\n\n"
    "如果对话只是表情/问候/无信息量 → 输出 []。\n"
    "如果只是单方面问询无回应 → 仅提取问询本身 (1-2 条)。"
)


PROMPT_BY_TYPE = {
    "chitchat": PROMPT_CHITCHAT,
    "planning": PROMPT_PLANNING,
    "sharing": PROMPT_SHARING,
    "argument": PROMPT_ARGUMENT,
    "unresolved_query": PROMPT_UNRESOLVED,
    "informative": PROMPT_INFORMATIVE,
}


def get_prompt(batch_type: str) -> str:
    """Lookup prompt by type, fallback to PROMPT_INFORMATIVE."""
    return PROMPT_BY_TYPE.get(batch_type, PROMPT_INFORMATIVE)
