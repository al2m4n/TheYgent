import { Check, Copy } from "lucide-react";
import { useEffect, useState } from "react";
import { Button } from "./ui/button";

// A copy-able code block: the snippet in a scrollable <pre>, a Copy button that flips to a check
// for a moment. The clipboard call is optional-chained — jsdom and non-secure contexts lack it.
export function CopySnippet({ label, code }: { label: string; code: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), 1400);
    return () => clearTimeout(t);
  }, [copied]);
  return (
    <div className="relative rounded-md border bg-muted/50">
      <pre className="mono overflow-x-auto p-2.5 pr-10 text-[11px] leading-relaxed">{code}</pre>
      <Button
        size="icon-sm"
        variant="ghost"
        className="absolute top-1 right-1"
        aria-label={`Copy ${label}`}
        title={`Copy ${label}`}
        onClick={() => {
          void navigator.clipboard?.writeText(code).then(() => setCopied(true));
        }}
      >
        {copied ? <Check /> : <Copy />}
      </Button>
    </div>
  );
}
