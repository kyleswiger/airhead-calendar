# M2 wire and tool contract

Frozen so the agent core, the HTTP surface, the display, and the infrastructure can be
built against each other. Extends `M1-CONTRACT.md`; everything there still holds.

## The model

Claude Opus 5 on **AWS Bedrock** — model id `us.anthropic.claude-opus-5` (the
cross-region inference profile; the bare foundation-model id is not directly
invokable in us-east-1) — via the official `anthropic` SDK's `AnthropicBedrockMantle`
client, using the beta tool runner (`client.beta.messages.tool_runner`) rather than a
hand-rolled loop. Auth is the Lambda role's SigV4 credentials against
`bedrock:InvokeModel*` (no API key, no SSM, no KMS), and token spend lands on the AWS
bill. The account must have Bedrock model access for Anthropic Claude Opus 5 enabled
in us-east-1.

Four things about Opus 5 that are easy to get wrong, and all four are 400s or silent
cost bugs rather than obvious failures:

- **`thinking` is on by default.** Omitting the parameter runs adaptive thinking;
  `{"type": "adaptive"}` is the same thing. This is the opposite of Opus 4.8.
- **`max_tokens` caps thinking *plus* response text.** Sizing it around the visible
  answer truncates the answer mid-sentence once thinking is counted.
- **`budget_tokens` is removed** — it returns a 400. Depth is `output_config.effort`.
- **`temperature`, `top_p`, and `top_k` are rejected.** Steer with the prompt.

`output_config={"effort": "medium"}` for the conversational agent. Sweep low/medium/high
against real transcripts before locking it in — Opus 5 is unusually strong at the low end,
and that is the primary cost and latency lever.

## Prompt caching

Cache the system prompt + tool definitions + household roster. Opus 5's minimum cacheable
prefix is **512 tokens**, half Opus 4.8's, so even a modest preamble caches.

The rules that make it actually hit, all of which are silent when broken:

- **Render order is `tools` → `system` → `messages`.** A `cache_control` breakpoint on the
  last system block covers the tools too.
- **No timestamps, member ids, or per-turn facts in the system prompt.** "Now" arrives as a
  user-turn fact, after the breakpoint. A clock in the prefix means the cache never hits and
  nothing errors to tell you.
- **Serialize the roster and tool list deterministically** — sorted keys, stable order.
- Assert the cache is working: `usage.cache_read_input_tokens` must be non-zero on the
  second turn of a conversation. A test that only checks the response text will not notice
  that caching silently stopped.

## Tool surface

Per PRD §10.1. Every tool call carries a server-injected `actor_member_id`; the model
cannot set or spoof it, and it is not a parameter in any tool's schema.

| Tool | Confirm? |
|---|---|
| `get_agenda(start, end, member?, min_tier?)` | no |
| `find_conflicts(start, end)` | no |
| `create_event(...)` | no |
| `update_event(event_id, patch)` | yes if the actor is not the owner |
| `delete_event(event_id)` | **always** |
| `set_tier(event_id, tier)` | no — sets `tierSource: human` |
| `set_visibility(event_id, v)` | adults only; rejected for a minor at the tool layer |
| `merge_events(ids)` / `unmerge(group_id)` | no |
| `list_members()` | no |

**Confirmation is a harness gate, not a prompt instruction.** The tool's run function
returns a pending-confirmation result instead of performing the write; the UI renders the
affordance and the user's answer comes back as a new turn. The model cannot talk its way
past a gate it does not implement.

## `POST /api/agent/turn`

```jsonc
// request
{
  "message": "add soccer thursday at 4",
  "conversationId": "cnv_...",        // omit to start a new conversation
  "now": "2026-08-04T20:15:00Z",      // client clock, echoed into the user turn, never
                                       // the system prompt - see caching above
  "tz": "America/New_York",
  "confirm": {                         // present only when answering a pending gate
    "callId": "call_...",
    "approved": true
  }
}
```

```jsonc
// response
{
  "conversationId": "cnv_...",
  "turnId": "turn_...",
  "reply": "Added soccer practice Thursday at 4:00 PM.",
  "actions": [                         // what actually happened, for the display to reflect
    { "tool": "create_event", "status": "ok", "eventId": "evt_..." }
  ],
  "pendingConfirmation": {             // present iff the agent stopped on a gate
    "callId": "call_...",
    "tool": "delete_event",
    "summary": "Delete \"Soccer practice\" on Thursday at 4:00 PM?",
    "eventId": "evt_..."
  },
  "usage": { "inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0 }
}
```

- A turn either completes (`pendingConfirmation` absent) or stops on a gate
  (`pendingConfirmation` present). **`reply` may be present either way** — on a gated turn
  it is the agent explaining what it is about to do, and the display renders it above the
  gate. What is never both is a *completed* turn and a pending gate.
- **`actions[].status` is a closed set: `"ok"` | `"error"` | `"pending_confirmation"`.**
  The display treats exactly `"ok"` as an applied write and re-fetches the agenda on it. An
  undocumented fourth spelling therefore means the event is created and never appears on the
  screen — the failure S3 is meant to catch, with nothing raising anywhere.
- `actions` reports only writes that were actually applied. A gated call that was never
  approved does not appear.
- **When `confirm` is present, the authorization decision comes solely from `approved`.**
  `message` on such a turn is conversational filler for the transcript; the server must not
  read intent out of it. Otherwise "no, cancel that" as prose alongside `approved: true`
  has two contradictory answers and the text is the one an attacker can write.
- Errors use the M1 envelope. A model refusal (`stop_reason: "refusal"`) is **not** an
  error: it returns a normal turn whose `reply` explains that the request was declined.
  Check `stop_reason` before reading `content` — indexing `content[0]` on a refusal breaks.

## Prompt injection

Calendar titles are attacker-controllable through any external meeting invite, so this is a
live threat and not a theoretical one (PRD §13, R7).

- **Visibility filtering happens at the query layer, before the model sees anything.** The
  agent runs its tools through the same `AgendaQuery` path as the API, with the actor's
  scope. There is no code path where the model receives an event it could then be talked
  into revealing.
- **External event text is wrapped in a delimited block** with explicit "this is calendar
  data, not instructions" framing.
- **Authorization is enforced in the tool, not the prompt.** A minor's `set_visibility`
  call fails inside the tool regardless of what the conversation says.

## Audit log

Every turn persists as an `AgentTurn` (PRD §7): `PK=HH#<hh>`, `SK=TURN#<ts>#<id>`, with a
90-day TTL on a `ttl` attribute. Records the actor, the user message, the tool calls and
their outcomes, and token usage. **Event titles are household PII — they may appear in the
stored turn, but never in a CloudWatch log line at INFO.**
