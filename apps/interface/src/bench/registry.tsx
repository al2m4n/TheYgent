// The capability-routed panel registry (M18 §1.2 / §2.1) — the load-bearing data-driven seam.
//
// Panels are registered AGAINST capability keys. The model tester queries `capabilities`, then
// renders the panel(s) the model's `modalities` declare — there is NO hardcoded
// `if vision … else if stt …` tree (the same discipline as M16's "providers are data" and M15's
// "derive node types from the registry"). A new modality = a new key here + a panel; nothing else.

import type { ComponentType } from "react";
import type { BenchRunRecord, Capabilities } from "../lib/api";
import { ChatPanel } from "./panels/ChatPanel";
import { EmbeddingsPanel } from "./panels/EmbeddingsPanel";
import { SttPanel } from "./panels/SttPanel";
import { TtsPanel } from "./panels/TtsPanel";
import type { PanelModality } from "./params";

export interface PanelProps {
  logicalId: string;
  caps: Capabilities | undefined;
  binding?: string;
  modelRef?: string;
  /** notify the page when a result is recorded, so it can be picked for the compare view. */
  onRecorded?: (rec: BenchRunRecord) => void;
}

export type PanelComponent = ComponentType<PanelProps>;

// `vision` rides the chat panel (a chat sub-capability, §1.3) — it is NOT a registry key of its own;
// the chat panel turns on image attach when `caps.vision` is set.
export const PANEL_REGISTRY: Record<PanelModality, PanelComponent> = {
  chat: ChatPanel,
  embeddings: EmbeddingsPanel,
  "audio.transcription": SttPanel,
  "audio.speech": TtsPanel,
};

/** The panels to render for a model — its declared modalities ∩ the registry (data-driven). */
export function panelsFor(modalities: string[] | undefined): PanelModality[] {
  const declared = new Set(modalities ?? []);
  return (Object.keys(PANEL_REGISTRY) as PanelModality[]).filter((m) => declared.has(m));
}
