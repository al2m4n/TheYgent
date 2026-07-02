// Benchmark math — captured AT THE BENCH from the stream + the gateway's usage numbers (there is
// NO inference-side metric API). Everything here is PURE so it is proven against a deterministic
// synthetic timed stream: the live panels feed it samples stamped with
// `performance.now() - start`, the tests feed it a fixture.

export interface Usage {
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  // LiteLLM attaches a cost number to the response/usage; absent for a local engine with no pricing.
  cost?: number | null;
  response_cost?: number | null;
}

export interface ChatSample {
  /** ms since the request was sent. */
  atMs: number;
  /** the delta's text content in this chunk, if any (the first non-empty one marks TTFT). */
  content?: string;
  /** the final usage object, if this chunk carried one (OpenAI stream_options.include_usage). */
  usage?: Usage;
}

export interface BenchMetrics {
  /** time to first token (ms) — the latency that matters for interactivity. */
  ttftMs?: number;
  /** wall-clock total (ms). */
  totalMs: number;
  /** generation throughput — completion tokens ÷ the post-TTFT generation window. */
  tokensPerSec?: number;
  promptTokens?: number;
  completionTokens?: number;
  /** USD cost from the gateway (LiteLLM); absent for a local engine with no pricing. */
  cost?: number;
  // ── modality extras ──
  /** STT real-time factor: audio seconds ÷ processing seconds (>1 = faster than real time). */
  rtf?: number;
  /** TTS time-to-first-byte (ms). */
  ttfbMs?: number;
  /** TTS synthesis throughput — input characters ÷ total time. */
  charsPerSec?: number;
}

function lastUsage(samples: ChatSample[]): Usage | undefined {
  for (let i = samples.length - 1; i >= 0; i--) {
    if (samples[i].usage) return samples[i].usage;
  }
  return undefined;
}

/**
 * Compute chat/vision metrics from a timed stream of samples. `samples[i].atMs` is the
 * offset from the request send. TTFT is the first content-bearing chunk; throughput is measured over
 * the generation window (after TTFT) when we have it, else over the whole call. Completion tokens come
 * from `usage` when the gateway reports it, else fall back to a chunk count (clearly approximate).
 */
export function computeChatMetrics(samples: ChatSample[]): BenchMetrics {
  const totalMs = samples.length ? samples[samples.length - 1].atMs : 0;
  const firstContent = samples.find((s) => s.content && s.content.length > 0);
  const ttftMs = firstContent?.atMs;
  const usage = lastUsage(samples);
  const completionTokens =
    usage?.completion_tokens ?? samples.filter((s) => s.content && s.content.length > 0).length;
  const promptTokens = usage?.prompt_tokens;
  const cost = usage?.cost ?? usage?.response_cost ?? undefined;

  let tokensPerSec: number | undefined;
  if (completionTokens && completionTokens > 0) {
    // Throughput over the GENERATION window (TTFT→end) is the honest tok/s; fall back to total.
    const windowMs = ttftMs !== undefined && totalMs > ttftMs ? totalMs - ttftMs : totalMs;
    if (windowMs > 0) tokensPerSec = completionTokens / (windowMs / 1000);
  }
  return {
    ttftMs,
    totalMs,
    tokensPerSec,
    promptTokens,
    completionTokens: completionTokens || undefined,
    cost: cost ?? undefined,
  };
}

/** STT metric: real-time factor = audio length ÷ processing time. */
export function computeSttMetrics(audioSeconds: number, processingMs: number): BenchMetrics {
  const rtf = processingMs > 0 ? audioSeconds / (processingMs / 1000) : undefined;
  return { totalMs: processingMs, rtf };
}

/** TTS metric: TTFB (first audio byte) + total + chars/sec. */
export function computeTtsMetrics(chars: number, ttfbMs: number, totalMs: number): BenchMetrics {
  const charsPerSec = totalMs > 0 ? chars / (totalMs / 1000) : undefined;
  return { totalMs, ttfbMs, charsPerSec };
}

/** Numeric metric deltas b−a over the keys both sides report (the compare view). */
export function metricDeltas(a: BenchMetrics, b: BenchMetrics): Record<string, number> {
  const out: Record<string, number> = {};
  for (const key of Object.keys(a) as (keyof BenchMetrics)[]) {
    const va = a[key];
    const vb = b[key];
    if (typeof va === "number" && typeof vb === "number") out[key] = vb - va;
  }
  return out;
}
