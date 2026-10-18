# Prompt-cache reuse across an agent's model calls — results

`measure.py` replays one agent the way the walker runs it. The first call sends the system prompt
(about 750 tokens of instructions), four tool schemas and the user's question. Two tool rounds
follow, each appending the walker's own `assistant_tool_calls_message` and `tool_result_message`.
A second run asks the same agent a different question. Every call streams with
`stream_options.include_usage`, as the walker does. `cached_tokens` is the engine's own count of
prompt tokens served from its prompt cache.

## Qwen3-0.6B-4bit (a plain-attention model), mlx-lm 0.31.3

| Run · step | Prompt tokens | Cached, direct to `mlx_lm.server` | Cached, through the inference plane | Time to first token (plane) |
|---|---:|---:|---:|---:|
| 1 · 1 | 921 | 0 | 0 | 1.59 s (includes starting the engine) |
| 1 · 2 | 1,220 | 921 | 921 | 0.20 s |
| 1 · 3 | 1,520 | 1,220 | 1,220 | 0.21 s |
| 2 · 1 | 923 | 892 | 892 | 0.11 s |
| 2 · 2 | 1,222 | 923 | 923 | 0.20 s |
| 2 · 3 | 1,522 | 1,222 | 1,222 | 0.21 s |

- **Each step reuses everything before it.** Step 2 is served 921 tokens from cache, exactly step 1's
  prompt, and processes only the new assistant tool call and tool result. A new run of the same agent
  reuses the system prompt and tools (892 of 923).
- **The inference plane preserves it.** The plane, through LiteLLM, sends the same prompt and
  reports the same counts as calling the engine directly.

## Why the reuse works here, and where it can stop

mlx-lm's server keeps up to 10 cached prompt states (`--prompt-cache-size`) and reuses the longest
one that matches the new prompt's start:

- **The cached state covers the prompt plus the generated reply.** The walker doesn't send the reply
  back verbatim. It rebuilds the assistant turn (content `None`, arguments re-serialized, reasoning
  dropped), so the chat template renders it differently from what the model produced, and the
  cached sequence diverges at the start of that turn.
- **Plain-attention models (`KVCache`) can be cut back** to the shared start, which is what the
  table shows: exactly the previous prompt is reused.
- **Hybrid models can't be cut back.** Qwen3.5/3.6 add linear-attention layers whose cache
  (`ArraysCache`) is a running state, not a list of tokens. Their reuse depends on mlx-lm having
  saved a checkpoint at a message boundary (its batched path does this) or on the new prompt
  extending an earlier one exactly. Measured below with Qwen3.6-35B-A3B.
- **A request with a `seed` takes mlx-lm's unbatched path**, which saves no message-boundary
  checkpoints.

## Qwen3.6-35B-A3B-4bit (a hybrid MoE model), mlx-lm 0.31.3, 36 GB M3 Pro

The same replay directly against `mlx_lm.server`, once with its default prompt cache and once with it
off (`--prompt-cache-size 0`). Each call generates up to 48 tokens.

| Run · step | Prompt tokens | Cached (cache on) | Time to first token, cache on | Time to first token, cache off |
|---|---:|---:|---:|---:|
| 1 · 1 | 1,042 | 0 | 2.26 s | 2.14 s |
| 1 · 2 | 1,352 | 1,041 | 0.88 s | 2.60 s |
| 1 · 3 | 1,664 | 1,351 | 0.88 s | 3.06 s |
| 2 · 1 | 1,044 | 1,011 | 0.30 s | 2.13 s |
| 2 · 2 | 1,354 | 1,043 | 0.87 s | 2.57 s |
| 2 · 3 | 1,663 | 1,353 | 0.85 s | 3.04 s |

- **The hybrid model reuses its cache too.** Its linear-attention state can't be cut back, but
  mlx-lm's batched path saves a checkpoint at each message boundary, so every step resumes from the
  end of the previous prompt (1,041 of 1,352, then 1,351 of 1,664). A new run of the same agent
  resumes after the system prompt and tools (1,011 of 1,044).
- **Prompt processing runs at about 500 tokens/s** on this M3 Pro (1,042 tokens in 2.1 s; 1,664 in
  3.1 s). With reuse, a tool step processes only the new tool call and result (~310 tokens here),
  in about 0.87 s.
- **Waiting for the first token across one three-step run:**
  - without reuse: 7.8 s;
  - with reuse, the agent's first run: 4.0 s (−49%);
  - with reuse, a repeat run of the same agent: 2.0 s (−74%).
- **Prompt processing dominates short steps.** Without reuse it's 2–3 s of a 3–4 s step that
  generates 48 tokens. Longer answers shift the balance toward generation (about 53 tokens/s for
  this model fully loaded on MLX).
- **The prompt cache is small:** 10 sequences took 0.88 GB here.

## Conclusions

- **No fix is needed for reuse.** TheYgent's agent calls already get prompt-cache reuse from
  mlx-lm, on plain and hybrid models alike, directly and through the inference plane.
- **Possible follow-ups, both small:**
  - mlx-lm's default of 10 cached sequences can be outgrown by many agents or concurrent runs on
    one model, which would turn reuse into misses. A `promptCacheSize` launch setting would let
    a busy model keep more.
  - An agent that sets `seed` takes mlx-lm's unbatched path, which saves no message-boundary
    checkpoints, so hybrid models would lose reuse. This is worth a note in the model-parameters
    docs.
- **A full agent run through the control plane wasn't done.** The replay uses the walker's own
  message helpers, so the requests have the same shapes.
