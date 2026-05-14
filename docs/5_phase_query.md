# Five-phase state inference, worked example

**English** · [中文](5_phase_query.zh.md)

> A single semantic recall can find *posts about a topic*. It cannot
> infer *the user's current state* with respect to that topic. The
> five-phase workflow combines five orthogonal signals to answer state
> questions like *"is project X still active"* or *"did I drop course
> Y"*.
>
> Below is a fully worked example. Names have been replaced with
> generic placeholders.

## Question

> *"Course Y — am I still attending it?"*

Background: the user enrolled in course Y at the start of the semester.
A semester later they want to confirm whether they ever dropped it.
A single `quick("Y")` returns ~30 cards (announcements, classmate
chatter) but cannot answer the *state* question.

## Phase A — seed

```bash
python -m src.core.memory_query quick "Course Y" --max-k 30
python -m src.core.memory_query arc  "Course Y"  --max-cards 80
```

Result:

- 11 cards in the chat group `<COURSE-Y-GROUP>`.
- Date range: `2026-03-10` (group joined) through `2026-04-23` (last
  observed activity).
- The user is one of ~30 group members.

## Phase B — entity expansion

Identify who else is in the group, and what their role is.

```bash
python -m src.core.memory_query arc "<TA-handle>"      --max-cards 20
python -m src.core.memory_query arc "<peer-handle>"    --max-cards 20
```

- `<TA-handle>` posted 7 homework notices in the same group → role = TA.
- `<peer-handle>` appears in another shared group (`<dept-alumni-group>`)
  → role = department peer.

## Phase C — five orthogonal signals

| # | Signal             | Query                                                | Result for course Y                                  |
|---|--------------------|------------------------------------------------------|------------------------------------------------------|
| 1 | user-speaks        | `quick("Course Y")` filtered to `speaker_role=self`  | 0 user-authored messages in the course chat          |
| 2 | user-silence       | `timeline(room=<COURSE-Y-GROUP>) ∩ user`             | User has never posted in this room                   |
| 3 | boundary           | `quick("drop deadline cutoff")`                      | Hit: drop deadline 18:00 on 2026-03-13, dean signs   |
| 4 | peer-triangulation | `arc("<peer-handle>") ∩ "Course Y"`                  | Peer and user privately discussed "申请通过" 2026-04-28 |
| 5 | private            | `quick("Course Y review")` filtered to `speaker_role=self` | 2026-04-03 self-note: "review Course Y for finals"   |

## Phase D — inference chain

```
2026-03-10  User joins the course group (enrolment success)
2026-03-13  Drop deadline 18:00, dean's signature required
2026-03-26  TA warns: "attendance too low, names will be called"
2026-04-03  User self-notes: "review Course Y"     ← still intending to take it
2026-04-22  Other students request leave (course continues)
2026-04-28  User + peer privately discuss "application approved"
2026-04-29  User confirms: "mine is approved"

Alignment:
  Peer is a department peer in the same cohort (Phase B inferred from
  the dept-alumni group). "Application approved" + same cohort +
  vague institutional process + user disappears from the course chat
  after April = drop request.
```

## Phase E — counter-evidence

```
Counter 1: if the user did not drop, after late April there should be
  - user replies in the course chat        → 0 cards
  - private griping about difficulty       → 0 cards
  - end-of-semester finals notes           → 0 cards
  All zero → drop conclusion stands.

Counter 2: could "application approved" mean something other than drop?
  - User and peer on 2026-04-29: "mine was approved without contacting
    professor". This is an institutional procedure with normal vs
    dean-signed paths — matches drop, not the other candidates
    (summer-research / direct-PhD applications never reference
    dean-signed paths).
  → Drop is the unique fit.
```

## Conclusion

> **The user dropped course Y.** First-drop privilege used; one drop
> still remaining.

## Why a single recall fails

If you only ran signal 1 ("the user does not post in the chat") you
would conclude "the user is silently auditing the class". If you ran
1 + 2 you would conclude "the user dropped silently but stays on the
roster". You need at least signals 1 + 2 + 4 + the counter-evidence
sweep to land on the unique state.

This pattern generalises to any *yes / no* question where the user has
left the social context. The template:

```
A. Seed cards   → identify the social context
B. Expand       → label other actors in that context
C. Five signals → user-speaks / user-silence / boundary / peer / private
D. Chain        → most likely state
E. Counter      → falsifying queries
```

## See also

- [usage_guide.md](usage_guide.md) — the eight subcommands used above.
- [architecture.md#6-five-phase-semantic-state-inference](architecture.md#6-five-phase-semantic-state-inference) — design rationale.
