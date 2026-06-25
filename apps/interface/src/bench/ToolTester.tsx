// The tool / MCP tester (M18 §2.6) — test a registered MCP tool standalone, the way you'd test a
// model. But a tool is not a data-plane model, so it runs through the AGENT/RUN path, not the data
// plane: we compose a THROWAWAY `input → mcp_tool → output` graph (`buildToolGraph`) and run it via
// `api.runGraph` (the existing inline-IR `/graphs/runs`). This adds NO new backend and NO new
// execution path — it is the agent bench pointed at a single tool. Classic-CV tools (YOLO/SAM/OCR)
// run as external MCP servers (§1.3), so their structured detections render through the SAME shared
// DetectionOverlay a grounding VLM uses (§2.2) — "see boxes on the feed" works for both.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Button, Card, Empty, ErrorBanner, Field, Input, Select, Spinner } from "../components/ui";
import { api } from "../lib/api";
import { type Detection, DetectionOverlay, detectionsFromOutput } from "./DetectionOverlay";
import { buildToolGraph } from "./toolgraph";

type InputMode = "image" | "json";

export function ToolTester() {
  const servers = useQuery({ queryKey: ["mcpServers"], queryFn: () => api.listMcpServers() });
  const [server, setServer] = useState("");
  const [tool, setTool] = useState("");
  const [mode, setMode] = useState<InputMode>("image");
  const [imageArg, setImageArg] = useState("image");
  // The uploaded image as a data URL — also the background the detection boxes draw over.
  const [imageSrc, setImageSrc] = useState<string | null>(null);
  const [json, setJson] = useState("");
  const [normalized, setNormalized] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detections, setDetections] = useState<Detection[] | null>(null);
  const [rawOutput, setRawOutput] = useState<string | null>(null);

  function onFile(file: File | undefined) {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => setImageSrc(typeof reader.result === "string" ? reader.result : null);
    reader.readAsDataURL(file);
  }

  async function run() {
    setError(null);
    setDetections(null);
    setRawOutput(null);
    // Compose the run input object + the arg names the throwaway graph templates from it
    // ($in.in.<name>): image mode wraps the data URL under the tool's image arg; JSON mode is a
    // free object of named args (an OCR/text tool, a tool whose input isn't an image).
    let input: Record<string, unknown>;
    if (mode === "image") {
      if (!imageSrc) {
        setError("Attach an image first.");
        return;
      }
      input = { [imageArg.trim() || "image"]: imageSrc };
    } else {
      try {
        const parsed = JSON.parse(json);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
          setError("JSON input must be an object of named tool args.");
          return;
        }
        input = parsed as Record<string, unknown>;
      } catch (e) {
        setError(`Invalid JSON: ${e instanceof Error ? e.message : String(e)}`);
        return;
      }
    }

    const ir = buildToolGraph({ server, tool: tool.trim(), argNames: Object.keys(input) });
    setRunning(true);
    try {
      const r = await api.runGraph({ ir, input });
      if (r.error) {
        setError(r.error);
        return;
      }
      setRawOutput(r.output ?? "");
      // Render the tool's structured output through the shared overlay (§2.6) — non-detection
      // output simply parses to no boxes and shows as raw text below.
      setDetections(detectionsFromOutput(r.output));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  }

  const canRun =
    Boolean(server && tool.trim()) && (mode === "image" ? Boolean(imageSrc) : Boolean(json.trim()));

  return (
    <section className="space-y-3">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400">Tool tester</h2>
      {servers.isLoading && <Spinner label="Loading MCP servers…" />}
      {servers.error && <ErrorBanner error={servers.error} />}
      {servers.data && servers.data.length === 0 && (
        <Empty>No registered MCP servers. Register one (e.g. a YOLO/SAM CV server) first.</Empty>
      )}
      {servers.data && servers.data.length > 0 && (
        <Card className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <Field label="MCP server">
              <Select value={server} onChange={(e) => setServer(e.target.value)}>
                <option value="">Pick a server…</option>
                {servers.data.map((s) => (
                  <option key={s.name} value={s.name} disabled={!s.connected}>
                    {s.name} {s.connected ? "" : "(disconnected)"}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Tool">
              <Input
                value={tool}
                onChange={(e) => setTool(e.target.value)}
                placeholder="detect / segment / ocr…"
              />
            </Field>
          </div>

          <div className="flex items-center gap-2 text-xs">
            <Button
              variant={mode === "image" ? "primary" : "ghost"}
              onClick={() => setMode("image")}
            >
              Image
            </Button>
            <Button variant={mode === "json" ? "primary" : "ghost"} onClick={() => setMode("json")}>
              JSON args
            </Button>
          </div>

          {mode === "image" ? (
            <div className="grid grid-cols-2 gap-3">
              <Field label="Image">
                <input
                  type="file"
                  accept="image/*"
                  onChange={(e) => onFile(e.target.files?.[0])}
                  className="w-full text-sm text-slate-300 file:mr-3 file:rounded-md file:border-0 file:bg-slate-800 file:px-3 file:py-1.5 file:text-slate-200"
                />
              </Field>
              <Field label="Image arg name">
                <Input
                  value={imageArg}
                  onChange={(e) => setImageArg(e.target.value)}
                  placeholder="image"
                />
              </Field>
            </div>
          ) : (
            <Field label="JSON args (object of named tool args)">
              <textarea
                value={json}
                onChange={(e) => setJson(e.target.value)}
                placeholder='{ "text": "hello" }'
                rows={4}
                className="w-full rounded-md border border-slate-700 bg-[#0e131c] px-2.5 py-1.5 font-mono text-sm text-slate-100 outline-none focus:border-blue-500"
              />
            </Field>
          )}

          <label className="flex items-center gap-2 text-xs text-slate-400">
            <input
              type="checkbox"
              checked={normalized}
              onChange={(e) => setNormalized(e.target.checked)}
            />
            Boxes are normalized (0–1 fractions) rather than pixels
          </label>

          <div className="flex items-center gap-2">
            <Button variant="primary" onClick={run} disabled={running || !canRun}>
              {running ? "Running…" : "Run tool"}
            </Button>
            {detections && (
              <span className="text-xs text-slate-500">{detections.length} detections</span>
            )}
          </div>

          {error && <p className="text-sm text-red-400">{error}</p>}

          {imageSrc && detections && detections.length > 0 && (
            <DetectionOverlay src={imageSrc} detections={detections} normalized={normalized} />
          )}
          {rawOutput && (
            <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded-md border border-slate-800 bg-[#0e131c] p-3 text-xs text-slate-300">
              {rawOutput}
            </pre>
          )}
        </Card>
      )}
    </section>
  );
}
