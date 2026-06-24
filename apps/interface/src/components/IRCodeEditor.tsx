// The "Code" view: the SAME IRDocument the canvas renders, shown as editable JSON. This is a second
// editor over the one source of truth — not a separate store. Editing valid JSON commits straight to
// the IR (`onChange`); invalid JSON is held locally and surfaced as an error, never committed, so the
// app state is always a real IRDocument. We show the FULL document (including `view`) so the round
// trip is honest and structure added here auto-lays-out when you switch back to Visual.
//
// A plain textarea — no editor library (mirrors the inspector's JSON fields). The frontend still
// never computes the contentHash or canonicalizes; this just edits the document the server hashes.

import type { IRDocument } from "@theygent/ir-types";
import { useEffect, useRef, useState } from "react";
import { Button } from "./ui";

function stringify(ir: unknown): string {
  return JSON.stringify(ir, null, 2);
}

interface Props {
  ir: IRDocument;
  onChange: (ir: IRDocument) => void;
  /** Reports whether the current text parses to a valid IR object (drives the Save gate). */
  onValidityChange?: (valid: boolean) => void;
}

export function IRCodeEditor({ ir, onChange, onValidityChange }: Props) {
  const [text, setText] = useState(() => stringify(ir));
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const copyTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  // The serialization of the IR THIS editor last emitted — so an external change (visual edit, load,
  // Revert) re-seeds the textarea, but our own commits don't clobber what the user is typing.
  const lastEmitted = useRef(stringify(ir));

  // The seed text mirrors a valid IR — report that on mount so the Save gate clears when this view
  // is (re-)opened, even if a previous session left an invalid-JSON state behind.
  useEffect(() => {
    onValidityChange?.(true);
  }, [onValidityChange]);

  useEffect(() => {
    const incoming = stringify(ir);
    if (incoming !== lastEmitted.current) {
      setText(incoming);
      lastEmitted.current = incoming;
      setError(null);
      onValidityChange?.(true);
    }
  }, [ir, onValidityChange]);

  const commit = (val: string) => {
    setText(val);
    try {
      const parsed = JSON.parse(val);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        setError("The IR must be a JSON object.");
        onValidityChange?.(false);
        return;
      }
      setError(null);
      onValidityChange?.(true);
      lastEmitted.current = stringify(parsed);
      onChange(parsed as IRDocument);
    } catch (e) {
      setError((e as Error).message);
      onValidityChange?.(false);
    }
  };

  const format = () => {
    try {
      commit(stringify(JSON.parse(text)));
    } catch {
      // invalid JSON can't be formatted — the error banner already says so.
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
    <div className="flex h-full min-h-0 flex-col bg-[#0b0e14]">
      <div className="flex items-center gap-2 border-b border-slate-800 px-3 py-1.5">
        <span className="text-[11px] font-medium uppercase tracking-wide text-slate-500">
          IR · JSON
        </span>
        {error ? (
          <span className="text-[11px] text-red-400">✗ invalid JSON — not applied</span>
        ) : (
          <span className="text-[11px] text-emerald-500">✓ applied</span>
        )}
        <div className="ml-auto flex gap-2">
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

      <textarea
        spellCheck={false}
        value={text}
        onChange={(e) => commit(e.target.value)}
        className={`mono min-h-0 flex-1 resize-none bg-[#0b0e14] px-3 py-2 text-xs leading-relaxed text-slate-100 outline-none ${
          error ? "text-slate-100" : ""
        }`}
        style={{ whiteSpace: "pre", overflowWrap: "normal", tabSize: 2 }}
      />

      {error && (
        <div className="border-t border-red-900 bg-red-950 px-3 py-1.5 text-[11px] text-red-200">
          {error}
        </div>
      )}
    </div>
  );
}
