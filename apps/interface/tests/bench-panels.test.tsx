// RTL render guards for capability-routed panels. Proves the capability-routed panels render
// the right inputs DRIVEN BY capabilities (no hardcoded modality switch): a chat+vision model shows
// the chat composer WITH image attach (upload + camera); the param form hides a capability the
// model lacks. The panels record sessions through react-query, so renders get a QueryClient.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";
import { ChatPanel } from "../src/bench/panels/ChatPanel";
import { PANEL_REGISTRY, panelsFor } from "../src/bench/registry";

function renderWithQuery(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe("capability-routed rendering", () => {
  it("a chat+vision model renders the chat panel WITH image attach", () => {
    const [modality] = panelsFor(["chat", "vision"]);
    const Panel = PANEL_REGISTRY[modality];
    renderWithQuery(
      <Panel logicalId="vlm" caps={{ vision: true, modalities: ["chat", "vision"] }} />,
    );
    expect(screen.getByText(/System \(optional\)/i)).toBeInTheDocument();
    // vision sub-capability is on: both attach paths show
    expect(screen.getByLabelText(/Attach image/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Capture from camera/i)).toBeInTheDocument();
  });

  it("a chat-only model renders the chat panel WITHOUT image attach", () => {
    renderWithQuery(<ChatPanel logicalId="chat" caps={{ vision: false, modalities: ["chat"] }} />);
    expect(screen.getByText(/System \(optional\)/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Attach image/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Capture from camera/i)).not.toBeInTheDocument();
  });

  it("the param form hides a capability-gated param the model lacks", () => {
    renderWithQuery(
      <ChatPanel logicalId="chat" caps={{ structuredOutput: false, modalities: ["chat"] }} />,
    );
    expect(screen.getByText(/Temperature/i)).toBeInTheDocument();
    expect(screen.queryByText(/JSON \/ structured output/i)).not.toBeInTheDocument();
  });
});
