# AGENTS.md — interface · `src/chat/`

The unified chat. Every conversation in the app (New Chat, a reopened session, the model bench's
chat panel, an agent conversation) renders through one shell, `ChatView`, driven by a
`ChatController` that one of two transports implements. Rules in
[apps/interface/AGENTS.md](../../AGENTS.md) apply first; the design is in "Unified chat" in
[interface.md](../../../../docs/dev-docs/interface.md).

## The two transports

| Transport | Talks to | Session history |
|-----------|----------|-----------------|
| [`useRunChat.ts`](useRunChat.ts) | The [control plane](../../../control-plane/AGENTS.md): `POST /runs` for a model, `POST /agents/{id}/runs` for an agent, streamed, with `session_id` | The server replays and appends turns |
| [`useInferenceChat.ts`](useInferenceChat.ts) | The [inference plane](../../../inference-plane/AGENTS.md)'s `/v1/chat/completions` directly (raw prompts and images never touch the control plane) | The client appends the finished pair via `POST /sessions/{id}/turns` |

## Map

<!-- agents-map: covers=. -->

| File | What it does |
|------|--------------|
| [`types.ts`](types.ts) | `ChatMessage`, `Attachment`, `ComposerCaps`, and the `ChatController` contract (`messages`, `busy`, `error`, `sessionId`, `composer`, `send`, `stop`, `disabled`) |
| [`ChatView.tsx`](ChatView.tsx) | The one shell: transcript plus composer for any controller |
| [`Composer.tsx`](Composer.tsx) | Text plus what the target's modality allows: image (upload or camera), audio (upload or microphone), file, or a validated JSON body |
| [`MessageBubble.tsx`](MessageBubble.tsx), [`Markdown.tsx`](Markdown.tsx) | One message (thinking block, markdown answer, attachments, media, per-turn metrics); GFM rendering with raw HTML escaped |
| [`ThinkingBlock.tsx`](ThinkingBlock.tsx), [`think.ts`](think.ts) | The collapsible reasoning block; `ThinkParser` splits one leading inline `<think>` block from a streamed answer |
| [`RunTraceBlock.tsx`](RunTraceBlock.tsx) | The per-turn run waterfall, folded under run-backed answers |
| [`CameraModal.tsx`](CameraModal.tsx), [`media.ts`](media.ts) | Camera snapshot and microphone capture; file → data URI |
| [`session.ts`](session.ts) | Session bookkeeping: create on first send, the two write paths |

## Rules specific to this folder

- A transport aborts its fetch on Stop, close and unmount, and records a deliberate stop first.
- What the composer offers and what a run body carries both come from `lib/modality.ts`
  (`boundaryOf`, `composerCapsFor`, `buildRunInput`); never per-media booleans.
- Reasoning never folds into the answer text or the stored turn.
- Agent audio travels as control-plane artifact refs (`/artifacts`); model chat media goes straight
  to the inference plane.

## Tests

`tests/run-chat.test.tsx`, `chat-replay.test.ts`, `chat-think.test.ts`,
`session-continue.test.tsx`.
