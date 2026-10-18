# MLX expert streaming — spike results

Model `mlx-community/Qwen3.6-35B-A3B-4bit`: 20.4 GB checkpoint (`qwen3_5_moe`), 40 MoE layers ×
256 experts, 8 routed per token, 1.77 MB per expert. Streaming leaves 18.1 GB of experts on disk
and keeps 1.39 GB resident. Measured on a 36 GB Apple Silicon Mac, mlx 0.31.2, mlx-lm 0.31.3.
Each run: the same 24-token chat prompt, 128 generated tokens, greedy (temperature 0). Every run's
tokens are identical to run 1's.

| # | Run | Decode tok/s | ms/token | Peak MLX memory | Expert hits | Disk read |
|---|-----|-------------:|---------:|----------------:|------------:|----------:|
| 1 | Fully loaded | 53.4 | 18.7 | 19.66 GB | — | — |
| 2 | Checkpoint layout, warm OS file cache | 7.7 | 130 | 1.76 GB | 0% (file cache) | 78 GB at 9.9 GB/s |
| 3 | Checkpoint layout, cold SSD | 3.5 | 283 | 1.76 GB | 0% | 78 GB at 3.0 GB/s |
| 4 | Checkpoint layout, cold SSD, 4 GB per-expert cache | 9.0 | 111 | 5.75 GB | 75% | 19.5 GB at 2.7 GB/s |
| 5 | Repacked, cold SSD | 4.4 | 227 | 1.76 GB | 0% | 78 GB at 4.0 GB/s |
| 6 | Repacked, cold SSD, 4 GB per-expert cache | 8.4 | 119 | 5.75 GB | 75% | 19.5 GB at 3.7 GB/s |
| 7 | Repacked, cold SSD, 16 read threads | 4.4 | 227 | 1.76 GB | 0% | 78 GB at 4.2 GB/s |
| 8 | Per-expert cache prewarmed to 100% hits | 23.8 | 42.0 | 19.96 GB | 100% | none |
| 9 | Slot bank prewarmed to 100% hits | 34.3 | 29.1 | 19.96 GB | 100% | none |
| 10 | Repacked, cold SSD, 4 GB slot bank (56 slots/layer) | 9.2 | 108 | 5.72 GB | 73% | 21.1 GB at 3.5 GB/s |
| 11 | Repacked, cold SSD, 8 layers pinned (3.6 GB), no cache | 5.1 | 195 | 5.38 GB | 0% (32 streamed layers) | 62 GB at 3.9 GB/s |

- *Checkpoint layout* reads each expert from the checkpoint's stacked tensors: nine reads per
  expert, six of them 32 KB. *Repacked* reads the `repack` output: each expert's nine tensors back
  to back in one page-aligned 1,769,472-byte record (an 18.12 GB file, written in 31 s), so a miss
  is one read.
- *Per-expert cache*: a global LRU of single-expert arrays, assembled into a compact set at every
  layer. *Slot bank*: per-layer preallocated GPU arrays the gather kernels read in place.
- *Pinned* layers keep the stock fully resident `SwitchGLU`: no streaming, no host sync.
- Cold runs start from files verified 0% cached. Raw results are in `results/`; the two
  `*-invalid.json` files are discarded runs (see Gotchas).

## What the spike proves

- **Exact.** Streamed generation is token-for-token identical to the fully loaded model in every
  configuration. The tiny synthetic self-test is bit-identical on logits in 18 variants (both
  layouts × no cache, file cache bypassed, per-expert cache, eviction pressure, prewarmed cache,
  slot bank, bank overflow during prefill, prewarmed bank, pinned layers).
- **Bounded memory.** Resident memory is the dense part plus the cache budget: 1.76 GB peak with no
  cache, about 5.7 GB with a 4 GB cache, against 19.66 GB fully loaded.
- **The hook point holds.** Replacing `SwitchGLU` after a lazy `load_model` is enough; the swapped-out
  expert arrays are never evaluated, and mlx-lm's generation loop runs unchanged.

## What each piece costs (decode, per token)

- **The host round trip is cheap.** At 100% hits the slot bank runs at 29.1 ms/token against the
  fully loaded 18.7 ms: 10.4 ms/token for 40 host syncs plus the index remap, about 0.26 ms per MoE
  layer. An earlier reading of runs 5–6 attributed most of the 61–73 ms routing wait to the round
  trips; runs 8–9 show that was wrong. The routing wait counts everything queued before each sync,
  including miss handling and prefill.
- **Per-expert assembly is the bigger fixed cost.** The per-expert cache at 100% hits is 42.0
  ms/token: 12.9 ms/token more than the bank, spent concatenating cached experts into a compact set
  at every layer. Its prefill took 23 s against the bank's 2.3 s for the same reason (one copy per
  cached expert per tensor, about 190 experts per layer).
- **Misses dominate everything else.** The 4 GB bank (run 10) is 108 ms/token, 79 ms/token above
  the same bank at 100% hits: about 47 ms/token of SSD reads plus about 32 ms/token of miss handling
  (host-to-mlx conversion per tensor per read chunk, slot writes, and the ops around them).
- **Hit rate per byte decides between caching and pinning.** 3.6 GB of pinned layers (20% of the
  experts, no syncs on 8 layers) gives 5.1 tok/s; 4 GB of slot bank (22% of the experts, the most
  recently used ones) gives 9.2 tok/s at a 73% hit rate. Routing is skewed enough that caching
  popular experts beats removing syncs.
- **Repacking pays off only when the disk is the bottleneck**: +26% with no cache (3.5 → 4.4 tok/s),
  no measurable gain once 75% of experts are cache hits.
- **More reader threads do not help decode**: a decode step issues one read per routed expert, so at
  most 8 reads per layer are in flight before the next host sync. The router's top-k caps the SSD
  queue depth.

## Levers for M1, in expected order of payoff

1. **Cache budget and hit rate.** Misses cost about 79 ms/token at 4 GB; every point of hit rate is
   worth more than any per-call optimization. Measure the hit-rate curve against budget (4, 8, 12
   GB) on long multi-turn sessions, and try frequency-aware eviction against plain LRU.
2. **Cheaper misses.** Store the bank as one raw record array per layer, with per-tensor views over
   it, so a miss is one host buffer, one conversion and one slot write instead of about 27 mlx ops.
3. **Deeper SSD queues.** Read ahead across layers (the next layer's likely experts) so more than
   top-k reads are in flight; on unified memory this competes with the GPU for memory bandwidth, so
   it has to be measured, not assumed.
4. Keep the slot bank (in-place hits, 2.3 s prefill) and the expert-major repack (one read per
   miss) as the defaults. Pinning whole layers is not worth its memory.

## Not covered

- Long prompts. A 2048-token prefill chunk routes to nearly every expert of every layer, which is a
  full 18 GB pass over the experts per chunk (about 4.5 s per chunk on a cold SSD at 4 GB/s).
- Repeat runs. Each configuration ran once; differences under about 10% are not meaningful.
- Concurrency: parallel requests route to more distinct experts and lower the hit rate.
- Models above memory size. This 20 GB model fits in 36 GB when fully loaded; it was streamed to
  measure the mechanism. The same path applies unchanged to a checkpoint larger than RAM.

## Gotchas found

- mlx streams belong to the thread that created them, and mlx-lm generates on its own stream. Reader
  threads must do pure I/O (`os.preadv` into host buffers); creating mlx arrays on them aborts with
  `There is no Stream(gpu, N) in current thread`.
- `F_NOCACHE` stops new caching but still serves pages already cached. A cold-SSD benchmark needs the
  files evicted first: `spike.py residency --evict` uses `msync(MS_INVALIDATE)` and verifies with
  `mincore`, without `sudo purge`. (`results/3-stream-ssd-warm-invalid.json` read from RAM at
  12 GB/s for this reason.)
- An array-indexed assignment (`bank[mx_indices] = rows`) is a scatter that copies the whole array:
  +453 MB peak and about 1–2 ms for a 453 MB bank on every update. A Python slice assignment
  (`bank[s:s + n] = rows`) updates in place (+4 MB peak), as long as every earlier read of the array
  has been evaluated.
- Queued fills keep their read buffers alive until evaluation. Prewarming all 40 banks before one
  `mx.eval` doubled the footprint and pushed the machine into swap
  (`results/9-bank-prewarmed-swapped-invalid.json`: 71 s load, 107 s to first token); evaluating each
  layer's fill before reading the next fixed it (11.6 s load, 2.3 s to first token). Runs at
  around 20 GB still grew swap by about 1.5 GB on this 36 GB machine with other work paused.
