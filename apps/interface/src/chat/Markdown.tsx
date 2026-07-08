// Markdown rendering for assistant messages — GFM (tables, task lists, strikethrough), styled
// with the app's slate ramp so it reads correctly in both themes. Kept intentionally lean: no
// syntax highlighting, no raw HTML (react-markdown escapes it by default — chat output is
// untrusted model text).

import type { ComponentProps } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { linkClass } from "../components/ui";

function Code({ className, children, ...props }: ComponentProps<"code">) {
  // Fenced blocks carry a language-* class and live inside our styled <pre>; inline code doesn't.
  if (className?.includes("language-")) {
    return (
      <code className={className} {...props}>
        {children}
      </code>
    );
  }
  return (
    <code className="mono rounded bg-slate-800/60 px-1 py-0.5 text-[0.9em]" {...props}>
      {children}
    </code>
  );
}

export function Markdown({ text }: { text: string }) {
  return (
    <div className="space-y-2 break-words text-sm leading-relaxed text-slate-200">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ children, href }) => (
            <a href={href} target="_blank" rel="noreferrer" className={linkClass}>
              {children}
            </a>
          ),
          code: Code,
          pre: ({ children }) => (
            <pre className="mono overflow-x-auto rounded-md border border-slate-800 bg-[var(--c-surface)] p-3 text-xs">
              {children}
            </pre>
          ),
          ul: ({ children }) => <ul className="list-disc space-y-1 pl-5">{children}</ul>,
          ol: ({ children }) => <ol className="list-decimal space-y-1 pl-5">{children}</ol>,
          h1: ({ children }) => <h1 className="text-base font-semibold">{children}</h1>,
          h2: ({ children }) => <h2 className="text-sm font-semibold">{children}</h2>,
          h3: ({ children }) => <h3 className="text-sm font-semibold">{children}</h3>,
          blockquote: ({ children }) => (
            <blockquote className="border-l-2 border-slate-700 pl-3 text-slate-400">
              {children}
            </blockquote>
          ),
          table: ({ children }) => (
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-xs">{children}</table>
            </div>
          ),
          th: ({ children }) => (
            <th className="border border-slate-800 bg-[var(--c-surface)] px-2 py-1 text-left font-semibold">
              {children}
            </th>
          ),
          td: ({ children }) => <td className="border border-slate-800 px-2 py-1">{children}</td>,
          hr: () => <hr className="border-slate-800" />,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
