# llama.cpp mixture-of-experts offload on Apple Silicon — results

Model `unsloth/Qwen3.6-35B-A3B-GGUF`, `UD-Q4_K_M` (22.1 GB), the same model the MLX expert-streaming
work measured. llama.cpp b11146 (Homebrew), 36 GB Apple Silicon Mac. Each run: the same 27-token
chat prompt, 128 generated tokens, temperature 0, context 4096, one slot. Cold runs start from the
model file evicted from the OS file cache (`measure.py residency --evict`).

| Run | Flags | Load | Prompt tok/s | Decode tok/s | Footprint | Expert weights live in | Output vs A |
|-----|-------|-----:|-------------:|-------------:|----------:|------------------------|-------------|
| A | default, cold | 26.2 s | 50.2 | 31.6 | 0.43 GB | the mapped file (GPU) | reference |
| D | `--no-warmup`, cold | 13.1 s | 111.4 | 34.1 | 0.43 GB | the mapped file (GPU) | identical |
| B | `--cpu-moe` (repack on, the default), file cached | 92.4 s | 0.46 | 0.64 | 19.0 GB | an 18.7 GB repacked copy (`CPU_REPACK`) + the mapped file | differs at char 489 of 500 |
| C | `--cpu-moe --no-repack --no-warmup`, cold | 11.6 s | 42.6 | 14.9 | 0.44 GB | the mapped file (CPU) | differs at char 489; identical to B |

*Footprint* is macOS's physical footprint: dirty, compressed and wired memory. Clean pages
mapped from the model file aren't counted, because the OS can drop them and re-read them from
the SSD. `results/C-invalid-repack-still-on.json` is a discarded run where a harness bug dropped
`--no-repack`. It thrashed like B.

## Findings

- **On a Mac, llama.cpp already keeps every weight mapped from the file.** In every configuration
  without repacking, the process's own footprint is about 0.4 GB for a 22 GB model. The weights,
  GPU-side included (`MTL0_Mapped`), are file pages the OS can reclaim. How well that holds up when
  a model is larger than memory isn't measured here: this model fits in 36 GB, so nothing had to
  be dropped. In every run the whole file ended up cached after load.
- **`--cpu-moe` doesn't help on Apple Silicon.** With repacking (the default), llama.cpp copies every
  expert into an 18.7 GB anonymous buffer while the file stays mapped for the GPU. That's about
  40 GB on a 36 GB machine, so it thrashed: 0.64 tok/s, and swap grew by about 16 GB. Without
  repacking it has the same footprint as the default and half the speed (14.9 vs 34 tok/s),
  because the experts run on the CPU.
- **Output changes when experts move to the CPU.** The two GPU runs match each other exactly, and
  so do the two CPU-expert runs, but the two groups diverge 489 characters in. Different kernels
  round differently. Unlike MLX expert streaming, llama.cpp's offload can't promise identical
  answers.
- **`--no-warmup` halves cold load time** (26 → 13 s) with identical output. The warm-up pass reads
  every weight once; skipping it leaves that to the first request.

## Memory-pressure run — stopped by a kernel panic

To check whether the memory-mapped default copes when a model doesn't fit, `hold_memory.py`
locked 20 GB of random bytes with `mlock` (23 GB wired in total). That left about 6 GB free or
reclaimable for the 22 GB model, and run P1 started in the default mode from a cold file.
llama.cpp began mapping the tensors (`load_tensors … load_mode = mmap`), and the machine never
recovered. Three minutes later macOS panicked (`watchdog timeout: no checkins from watchdogd in
94 seconds`) and restarted. P1 never finished loading, and P2 (`--cpu-moe --no-repack`) never ran.

- **What it shows:** with memory this short, llama.cpp's default GPU load on Apple Silicon doesn't
  degrade into slow paging. The machine stalls. Locked memory made it worse, because the OS couldn't
  page the holder out either, so this is a worst case rather than a typical one. But it gives no
  evidence that a GGUF model larger than memory can run on a Mac.
- **Don't repeat it this way.** Holding memory with `mlock` on a workstation can freeze it.
  `hold_memory.py` is kept only as the record of what was run.

## What this means for supporting llama.cpp

- **macOS:** turning on `--cpu-moe` would make things worse, and the default memory-mapped load
  stalls the machine when the model is far larger than the free memory. For an MoE model bigger
  than memory on a Mac, the MLX variant with expert streaming is the path. llama.cpp gets no
  streaming switch on macOS.
- **NVIDIA (Linux):** `--cpu-moe` / `--n-cpu-moe` are llama.cpp's standard way to run an MoE model
  bigger than GPU memory. Experts sit in system RAM and attention in VRAM. That path can't be
  verified on this machine.
