// The per-node "Code" view: a single graph node shown as an editable, syntax-highlighted JSON editor
// (CodeMirror 6) — the SAME editor the whole-graph Code view uses, scoped to one node. Editing valid
// JSON commits the node straight back to the graph (`onCommit`); invalid JSON is held locally and
// surfaced inline by the linter, never committed, so app state is always a real node. This IS the
// escape hatch for everything the form doesn't expose (e.g. a `contentHash` pin) — there's no
// separate config panel below the form.

import { autocompletion } from "@codemirror/autocomplete";
import { indentWithTab } from "@codemirror/commands";
import { json, jsonParseLinter } from "@codemirror/lang-json";
import { lintGutter, linter } from "@codemirror/lint";
import type { Node as IRNode } from "@theygent/ir-types";
import CodeMirror, { EditorView, keymap } from "@uiw/react-codemirror";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTheme } from "../lib/theme";
import { irCompletions } from "./IRCodeEditor";
import { Button } from "./ui";

function stringify(v: unknown): string {
  return JSON.stringify(v, null, 2);
}

export function NodeCodeEditor({
  node,
  onCommit,
}: {
  node: IRNode;
  onCommit: (node: IRNode) => void;
}) {
  const { resolved } = useTheme();
  const [text, setText] = useState(() => stringify(node));
  const [valid, setValid] = useState(true);
  const [copied, setCopied] = useState(false);
  const copyTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  // The serialization THIS editor last emitted — so an external change (a different node selected, a
  // wizard edit, load/revert) re-seeds the editor, but our own commit doesn't clobber what's typed.
  const lastEmitted = useRef(stringify(node));

  const extensions = useMemo(
    () => [
      json(),
      lintGutter(),
      linter(jsonParseLinter()),
      // Reuse the whole-graph editor's schema-aware suggestions (node `type`s, kind/channel enums,
      // property names) — they're just as relevant inside one node object.
      autocompletion({ override: [irCompletions] }),
      keymap.of([indentWithTab]),
      EditorView.lineWrapping,
    ],
    [],
  );

  // Re-seed from an EXTERNAL change (a different node, a wizard edit, Revert) — but not from our own
  // commit (which leaves incoming === lastEmitted), so the editor doesn't reformat / jump the cursor.
  useEffect(() => {
    const incoming = stringify(node);
    if (incoming !== lastEmitted.current) {
      setText(incoming);
      lastEmitted.current = incoming;
      setValid(true);
    }
  }, [node]);

  const commit = (val: string) => {
    setText(val);
    try {
      const parsed = JSON.parse(val);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        setValid(false);
        return;
      }
      setValid(true);
      lastEmitted.current = stringify(parsed);
      onCommit(parsed as IRNode);
    } catch {
      setValid(false);
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
  useEffect(() => () => clearTimeout(copyTimer.current), []);

  return (
    <div className="flex h-full min-h-0 flex-col bg-[var(--c-bg)]">
      <div className="flex items-center gap-2 border-b border-slate-800 px-3 py-1.5">
        <span className="text-[11px] font-medium uppercase tracking-wide text-slate-500">
          node · JSON
        </span>
        {valid ? (
          <span className="text-[11px] text-emerald-700 dark:text-emerald-500">✓ applied</span>
        ) : (
          <span className="text-[11px] text-red-700 dark:text-red-400">
            ✗ invalid — not applied
          </span>
        )}
        <div className="ml-auto flex gap-1.5">
          <Button className="h-7 text-xs" onClick={format} title="Re-indent the JSON">
            Format
          </Button>
          <div className="relative">
            <Button className="h-7 text-xs" onClick={copy} title="Copy the node to the clipboard">
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
          extensions={extensions}
          theme={resolved === "light" ? "light" : "dark"}
          height="100%"
          className="h-full text-[12px]"
        />
      </div>
    </div>
  );
}
