// The "Code" view: the SAME IRDocument the canvas renders, shown as an editable, syntax-highlighted
// JSON editor (CodeMirror 6). This is a second editor over the one source of truth — not a separate
// store. Editing valid JSON commits straight to the IR (`onChange`); invalid JSON is held locally and
// surfaced inline (the linter) + via the Save gate (`onValidityChange`), never committed, so the app
// state is always a real IRDocument. We show the FULL document (including `view`) so the round-trip is
// honest and structure added here auto-lays-out when you switch back to Visual.
//
// Rich-editor affordances come from CodeMirror's default basicSetup: JSON syntax highlighting,
// auto-indent on Enter, a fold gutter (collapse/expand each object/array), line numbers, bracket
// matching + auto-close, find (Cmd/Ctrl-F), and an undo history. Two linters annotate problems:
// JSON syntax (positioned) and the structural IR checks (mirrors the backend's validate_graph).

import {
  type CompletionContext,
  type CompletionResult,
  autocompletion,
} from "@codemirror/autocomplete";
import { indentWithTab } from "@codemirror/commands";
import { json, jsonParseLinter } from "@codemirror/lang-json";
import { foldAll, unfoldAll } from "@codemirror/language";
import { type Diagnostic, lintGutter, linter } from "@codemirror/lint";
import { type IRDocument, NODE_TYPES } from "@theygent/ir-types";
import CodeMirror, { EditorView, keymap } from "@uiw/react-codemirror";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTheme } from "../lib/theme";
import { validateGraph } from "../lib/validate";
import { Button } from "./ui";

function stringify(ir: unknown): string {
  return JSON.stringify(ir, null, 2);
}

// Structural IR linter: only runs once the JSON parses (syntax errors are reported, positioned, by
// jsonParseLinter). It mirrors the backend's validate_graph (lib/validate). Semantic issues have no
// source span, so they anchor to the document start — the backend stays authoritative at run time.
function irSemanticLinter(view: EditorView): Diagnostic[] {
  const text = view.state.doc.toString();
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return []; // jsonParseLinter owns syntax errors
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return [];
  return validateGraph(parsed as IRDocument).map((issue) => ({
    from: 0,
    to: Math.min(1, view.state.doc.length),
    severity: issue.severity,
    message:
      issue.message +
      (issue.nodeId
        ? `  ·  node "${issue.nodeId}"`
        : issue.edgeId
          ? `  ·  edge "${issue.edgeId}"`
          : ""),
  }));
}

// Schema-aware autocomplete, driven by the IR vocabulary: node `type`s come from the registry
// (NODE_TYPES — never hardcoded), plus the kind / channel / binding enums and the common property
// names. The relevant set is chosen from the key just before the cursor (a value context like
// `"type": "…`), else we offer property-name keys when typing a key.
const NODE_TYPE_NAMES = Object.keys(NODE_TYPES);
const KINDS = ["boundary", "activity", "orchestration"];
const CHANNELS = ["data", "control", "tool"];
const BINDINGS = ["mlx", "vllm", "llamacpp", "openai-compatible"];
const SOURCES = ["hf", "local-path", "url"];
const KEY_NAMES = [
  "schemaVersion",
  "id",
  "name",
  "version",
  "models",
  "tools",
  "nodes",
  "edges",
  "view",
  "type",
  "kind",
  "label",
  "config",
  "ports",
  "source",
  "sourceHandle",
  "target",
  "targetHandle",
  "channel",
  "condition",
  "binding",
  "model",
  "params",
  "messages",
  "role",
  "content",
];

export function irCompletions(context: CompletionContext): CompletionResult | null {
  const line = context.state.doc.lineAt(context.pos);
  const before = line.text.slice(0, context.pos - line.from);
  const valueCtx = (key: string) => new RegExp(`"${key}"\\s*:\\s*"[^"]*$`).test(before);

  let values: string[] | null = null;
  if (valueCtx("type")) values = NODE_TYPE_NAMES;
  else if (valueCtx("kind")) values = KINDS;
  else if (valueCtx("channel")) values = CHANNELS;
  else if (valueCtx("binding")) values = BINDINGS;
  else if (valueCtx("source")) values = SOURCES;

  if (values) {
    const word = context.matchBefore(/[\w.-]*/);
    return {
      from: word ? word.from : context.pos,
      options: values.map((v) => ({ label: v, type: "enum" })),
    };
  }

  // Property-name context: a key being typed right after `{` or `,`.
  if (/[{,]\s*"\w*$/.test(before)) {
    const word = context.matchBefore(/\w*/);
    return {
      from: word ? word.from : context.pos,
      options: KEY_NAMES.map((k) => ({ label: k, type: "property" })),
    };
  }
  return null;
}

interface Props {
  ir: IRDocument;
  onChange: (ir: IRDocument) => void;
  /** Reports whether the current text parses to a valid IR object (drives the Save gate). */
  onValidityChange?: (valid: boolean) => void;
}

export function IRCodeEditor({ ir, onChange, onValidityChange }: Props) {
  const { resolved } = useTheme();
  const [text, setText] = useState(() => stringify(ir));
  const [valid, setValid] = useState(true);
  const [copied, setCopied] = useState(false);
  const copyTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  // The live EditorView, captured on mount — so the Fold all / Unfold all buttons can run those
  // commands against it.
  const viewRef = useRef<EditorView | null>(null);
  // The serialization of the IR THIS editor last emitted — so an external change (visual edit, load,
  // Revert) re-seeds the editor, but our own commits don't clobber what the user is typing.
  const lastEmitted = useRef(stringify(ir));

  const extensions = useMemo(
    () => [
      json(),
      lintGutter(),
      linter(jsonParseLinter()),
      linter(irSemanticLinter),
      autocompletion({ override: [irCompletions] }),
      keymap.of([indentWithTab]),
      EditorView.lineWrapping,
    ],
    [],
  );

  // The seed text mirrors a valid IR — report that on mount so the Save gate clears when this view
  // is (re-)opened, even if a previous session left an invalid-JSON state behind.
  useEffect(() => {
    onValidityChange?.(true);
  }, [onValidityChange]);

  // Re-seed from an EXTERNAL change (visual edit, load, Revert) — but not from our own commit (which
  // leaves `incoming === lastEmitted`), so the editor doesn't reformat / jump the cursor as you type.
  useEffect(() => {
    const incoming = stringify(ir);
    if (incoming !== lastEmitted.current) {
      setText(incoming);
      lastEmitted.current = incoming;
      setValid(true);
      onValidityChange?.(true);
    }
  }, [ir, onValidityChange]);

  const commit = (val: string) => {
    setText(val);
    try {
      const parsed = JSON.parse(val);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        setValid(false);
        onValidityChange?.(false);
        return;
      }
      setValid(true);
      onValidityChange?.(true);
      lastEmitted.current = stringify(parsed);
      onChange(parsed as IRDocument);
    } catch {
      setValid(false);
      onValidityChange?.(false);
    }
  };

  const format = () => {
    try {
      commit(stringify(JSON.parse(text)));
    } catch {
      // invalid JSON can't be formatted — the inline linter already flags it.
    }
  };

  const copy = () => {
    void navigator.clipboard?.writeText(text);
    setCopied(true);
    clearTimeout(copyTimer.current);
    copyTimer.current = setTimeout(() => setCopied(false), 1400);
  };

  // Drop the pending "Copied!" reset if the editor unmounts (e.g. toggling back to Visual).
  useEffect(() => () => clearTimeout(copyTimer.current), []);

  return (
    <div className="flex h-full min-h-0 flex-col bg-[var(--c-bg)]">
      <div className="flex items-center gap-2 border-b border-slate-800 px-3 py-1.5">
        <span className="text-[11px] font-medium uppercase tracking-wide text-slate-500">
          IR · JSON
        </span>
        {valid ? (
          <span className="text-[11px] text-emerald-700 dark:text-emerald-500">✓ applied</span>
        ) : (
          <span className="text-[11px] text-red-700 dark:text-red-400">
            ✗ invalid JSON — not applied
          </span>
        )}
        <span className="hidden text-[11px] text-slate-600 lg:inline">
          · Tab indents · ⌘F find · type for suggestions
        </span>
        <div className="ml-auto flex gap-2">
          <Button
            className="!py-1 text-xs"
            onClick={() => viewRef.current && foldAll(viewRef.current)}
            title="Collapse every object/array"
          >
            Fold all
          </Button>
          <Button
            className="!py-1 text-xs"
            onClick={() => viewRef.current && unfoldAll(viewRef.current)}
            title="Expand everything"
          >
            Unfold all
          </Button>
          <Button className="!py-1 text-xs" onClick={format} title="Re-indent the JSON">
            Format
          </Button>
          <div className="relative">
            <Button className="!py-1 text-xs" onClick={copy} title="Copy the IR to the clipboard">
              Copy
            </Button>
            {copied && (
              <span className="-translate-x-1/2 absolute top-full left-1/2 mt-1 whitespace-nowrap rounded bg-emerald-600 px-1.5 py-0.5 text-[10px] font-medium text-white shadow-lg">
                Copied!
              </span>
            )}
          </div>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-hidden">
        <CodeMirror
          value={text}
          onChange={commit}
          onCreateEditor={(view) => {
            viewRef.current = view;
          }}
          extensions={extensions}
          theme={resolved === "light" ? "light" : "dark"}
          height="100%"
          className="h-full text-[12px]"
        />
      </div>
    </div>
  );
}
