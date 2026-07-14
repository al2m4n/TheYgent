// The tool / MCP tester — test a registered MCP tool standalone, the way you'd test a model. But a
// tool is not a data-plane model, so it runs through the AGENT/RUN path, not the data plane: we
// compose a THROWAWAY `input → mcp_tool → output` graph (`buildToolGraph`) and run it via
// `api.runGraph` (the existing inline-IR `/graphs/runs`). This adds NO new backend and NO new
// execution path — it is the agent bench pointed at a single tool. Classic-CV tools (YOLO/SAM/OCR)
// run as external MCP servers, so their structured detections render through the SAME shared
// DetectionOverlay a grounding VLM uses — "see boxes on the feed" works for both.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import {
  Button,
  Card,
  Empty,
  ErrorBanner,
  Field,
  Input,
  SectionHeading,
  Select,
  Spinner,
  Textarea,
} from "../components/ui";
import { api } from "../lib/api";
import { type Detection, DetectionOverlay, detectionsFromOutput } from "./DetectionOverlay";
import { buildToolGraph } from "./toolgraph";

type InputMode = "image" | "json";

export function ToolTester() {
  const servers = useQuery({ queryKey: ["mcpServers"], queryFn: () => api.listMcpServers() });
  const connections = useQuery({ queryKey: ["connections"], queryFn: () => api.listConnections() });
  // The target is a registered server OR an mcp_server connection — the value carries which
  // ("srv:<name>" / "con:<id>") so the throwaway graph binds the right config field.
  const [target, setTarget] = useState("");
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

    const ir = buildToolGraph({
      ...(target.startsWith("con:")
        ? { connection: target.slice(4) }
        : { server: target.slice(4) }),
      tool: tool.trim(),
      argNames: Object.keys(input),
    });
    setRunning(true);
    try {
      const r = await api.runGraph({ ir, input });
      if (r.error) {
        setError(r.error);
        return;
      }
      setRawOutput(r.output ?? "");
      // Render the tool's structured output through the shared overlay — non-detection output
      // simply parses to no boxes and shows as raw text below.
      setDetections(detectionsFromOutput(r.output));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  }

  const canRun =
    Boolean(target && tool.trim()) && (mode === "image" ? Boolean(imageSrc) : Boolean(json.trim()));

  const mcpConnections = (connections.data ?? []).filter((c) => c.kind === "mcp_server");
  const loaded = servers.data !== undefined && connections.data !== undefined;
  const hasTargets = (servers.data?.length ?? 0) > 0 || mcpConnections.length > 0;

  return (
    <section className="space-y-3">
      <SectionHeading>Tool tester</SectionHeading>
      {(servers.isLoading || connections.isLoading) && <Spinner label="Loading MCP servers…" />}
      {servers.error && <ErrorBanner error={servers.error} />}
      {loaded && !hasTargets && (
        <Empty>No MCP servers. Add one (e.g. a YOLO/SAM CV server) first.</Empty>
      )}
      {hasTargets && (
        <Card className="space-y-3 p-4">
          <div className="grid grid-cols-2 gap-3">
            <Field label="MCP server">
              <Select value={target} onChange={(e) => setTarget(e.target.value)}>
                <option value="">Pick a server…</option>
                {(servers.data ?? []).map((s) => (
                  <option key={`srv:${s.name}`} value={`srv:${s.name}`} disabled={!s.connected}>
                    server · {s.name} {s.connected ? "" : "(disconnected)"}
                  </option>
                ))}
                {mcpConnections.map((c) => (
                  <option key={`con:${c.id}`} value={`con:${c.id}`}>
                    connection · {c.name}
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
              <Textarea
                value={json}
                onChange={(e) => setJson(e.target.value)}
                placeholder='{ "text": "hello" }'
                rows={4}
                className="mono"
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

          {error && <ErrorBanner error={error} />}

          {imageSrc && detections && detections.length > 0 && (
            <DetectionOverlay src={imageSrc} detections={detections} normalized={normalized} />
          )}
          {rawOutput && (
            <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded-md border border-slate-800 bg-[var(--c-surface)] p-3 text-sm text-slate-200">
              {rawOutput}
            </pre>
          )}
        </Card>
      )}
    </section>
  );
}
