// M18 bench — RTL render guards (m18.md §4 frontend). Proves the capability-routed panels render
// the right inputs DRIVEN BY capabilities (no hardcoded modality switch): a chat+vision model shows
// the chat panel WITH image attach; the param form hides a capability the model lacks.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ChatPanel } from "../src/bench/panels/ChatPanel";
import { PANEL_REGISTRY, panelsFor } from "../src/bench/registry";

describe("capability-routed rendering", () => {
  it("a chat+vision model renders the chat panel WITH image attach", () => {
    const [modality] = panelsFor(["chat", "vision"]);
    const Panel = PANEL_REGISTRY[modality];
    render(<Panel logicalId="vlm" caps={{ vision: true, modalities: ["chat", "vision"] }} />);
    expect(screen.getByText(/Prompt/i)).toBeInTheDocument();
    expect(screen.getByText(/Image URL/i)).toBeInTheDocument(); // vision sub-capability is on
  });

  it("a chat-only model renders the chat panel WITHOUT image attach", () => {
    render(<ChatPanel logicalId="chat" caps={{ vision: false, modalities: ["chat"] }} />);
    expect(screen.getByText(/Prompt/i)).toBeInTheDocument();
    expect(screen.queryByText(/Image URL/i)).not.toBeInTheDocument();
  });

  it("the param form hides a capability-gated param the model lacks", () => {
    render(<ChatPanel logicalId="chat" caps={{ structuredOutput: false, modalities: ["chat"] }} />);
    expect(screen.getByText(/Temperature/i)).toBeInTheDocument();
    expect(screen.queryByText(/JSON \/ structured output/i)).not.toBeInTheDocument();
  });
});
