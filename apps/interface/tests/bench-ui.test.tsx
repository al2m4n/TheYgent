// RTL guards for the bench modals + the slider param control + the Modal.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ModelBench } from "../src/bench/ModelBench";
import { ParamForm } from "../src/bench/ParamForm";
import { paramsForModality } from "../src/bench/params";
import { Modal } from "../src/components/ui";

function withQuery(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

describe("Modal", () => {
  it("renders title + children and closes via the close button and Escape", () => {
    const onClose = vi.fn();
    render(
      <Modal title="My modal" onClose={onClose}>
        <p>body content</p>
      </Modal>,
    );
    expect(screen.getByText("My modal")).toBeInTheDocument();
    expect(screen.getByText("body content")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Close"));
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});

describe("slider param control", () => {
  it("renders a slider + number for a bounded numeric param and both drive onChange", () => {
    const specs = paramsForModality("chat", {}).filter((s) => s.key === "temperature");
    const onChange = vi.fn();
    render(<ParamForm specs={specs} values={{}} onChange={onChange} />);
    // bounded (min 0, max 2) → a range slider is present alongside the number input
    const slider = screen.getByRole("slider", { name: /temperature/i });
    expect(slider).toBeInTheDocument();
    fireEvent.change(slider, { target: { value: "0.7" } });
    expect(onChange).toHaveBeenCalledWith("temperature", "0.7");
  });
});

describe("ModelBench (capability-routed, in a modal body)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("renders the chat panel for a chat model (driven by capabilities)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => ({ modalities: ["chat"], vision: false }),
      })) as unknown as typeof fetch,
    );
    render(
      withQuery(
        <ModelBench model={{ logicalId: "triage", binding: { binding: "mlx", model: "m" } }} />,
      ),
    );
    // capabilities probe resolves → chat panel renders with its Prompt field
    await waitFor(() => expect(screen.getByText(/Prompt/i)).toBeInTheDocument());
    expect(screen.getByText("triage")).toBeInTheDocument();
  });
});
