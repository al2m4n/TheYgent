// The app-facing primitive set. The public API (and every call site) predates the component
// library underneath: these wrappers keep the app's compact geometry and tone system while the
// look, focus handling, and overlay behaviour come from the generated components in ./ui/*.
// New code can compose ./ui/* directly; existing surfaces go through these so a restyle stays
// a one-file change.

import {
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
  useEffect,
  useRef,
} from "react";
import { toneOf } from "../lib/categories";
import { statusClass } from "../lib/format";
import { cn } from "../lib/utils";
import { Alert, AlertDescription } from "./ui/alert";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "./ui/alert-dialog";
import { Badge as BaseBadge } from "./ui/badge";
import { Button as BaseButton, buttonVariants } from "./ui/button";
import { Card as BaseCard } from "./ui/card";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "./ui/dialog";
import { Empty as BaseEmpty, EmptyDescription } from "./ui/empty";
import { Input as BaseInput } from "./ui/input";
import { Progress } from "./ui/progress";
import { Spinner as SpinnerIcon } from "./ui/spinner";
import { Table as BaseTable, TableCell, TableHead } from "./ui/table";
import { Textarea as BaseTextarea } from "./ui/textarea";

type Variant = "primary" | "default" | "ghost" | "danger";

// The app's historical variant names, mapped onto the component library's: `primary` is the one
// blue action, `default` is the bordered neutral button, `danger` the soft-red destructive.
const VARIANT: Record<Variant, "default" | "outline" | "ghost" | "destructive"> = {
  primary: "default",
  default: "outline",
  ghost: "ghost",
  danger: "destructive",
};

// The button look as a class string, for elements that must stay real links (<Link>/<a>) but read
// as buttons — one source of truth with <Button>, so the two can never drift.
export function buttonClass(variant: Variant = "default", className = ""): string {
  return cn(buttonVariants({ variant: VARIANT[variant] }), className);
}

// The one link color, paired for both themes (blue is semantic — it does not ride the inverted
// slate ramp). Compose with `mono` etc. at the call site.
export const linkClass =
  "text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300";

export function Button({
  variant = "default",
  className = "",
  type = "button",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return <BaseButton type={type} variant={VARIANT[variant]} className={className} {...props} />;
}

export function Input({ className = "", ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <BaseInput className={className} {...props} />;
}

// A native <select> (callers drive it with value/onChange and plain <option>s), dressed in the
// same field chrome as Input so mixed forms read as one family.
export function Select({ className = "", ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      data-slot="select"
      className={cn(
        "h-8 w-full min-w-0 rounded-lg border border-input bg-transparent px-2.5 py-1 text-sm transition-colors outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-input/30",
        className,
      )}
      {...props}
    />
  );
}

export function Textarea({
  className = "",
  ...props
}: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  // Fixed sizing: callers drive height with `rows` and expect internal scroll. The generated base
  // uses content-driven sizing (field-sizing-content + min-h-16), which makes `rows` inert and
  // grows the element without bound as text accumulates — wrong for composers and param forms.
  return <BaseTextarea className={cn("field-sizing-fixed min-h-0", className)} {...props} />;
}

// The run status pill (Runs list + run detail) — completed green, failed red, streaming amber.
export function StatusBadge({ status }: { status: string }) {
  return (
    <BaseBadge variant="outline" className={cn("rounded-full", statusClass(status))}>
      {status}
    </BaseBadge>
  );
}

export function Badge({ children, tone = "slate" }: { children: ReactNode; tone?: string }) {
  // The tone palette is centralized in lib/categories so a Badge, a filter chip, and a table cell
  // all paint the same category the same colour. Unknown tones fall back to slate.
  return (
    <BaseBadge variant="secondary" className={cn("rounded px-1.5 text-[11px]", toneOf(tone).badge)}>
      {children}
    </BaseBadge>
  );
}

// The one section-within-a-page heading treatment (matches the Th/Field-label typography).
export function SectionHeading({
  children,
  className = "",
}: { children: ReactNode; className?: string }) {
  return (
    <h2
      className={cn(
        "text-xs font-semibold uppercase tracking-wide text-muted-foreground",
        className,
      )}
    >
      {children}
    </h2>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="block text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      {children}
    </label>
  );
}

// The standard content-page container. Every routed page wraps in this so the page width and the
// responsive side/top padding stay IDENTICAL across the app — change the gutter once, here, and it
// moves everywhere. Pages are full-width (no centered max-width cap); pass spacing via `className`
// (e.g. "space-y-4"). The canvas Editor is the one exception — it's a full-bleed, full-height tool
// view, not a scrolling content page, so it does not use Page.
export function Page({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`w-full px-4 py-6 sm:px-6 lg:px-8 ${className}`}>{children}</div>;
}

// A plain container in the card surface. Call sites own their internal layout and padding, so the
// stacked flex/gap/padding defaults are zeroed out here; `overflow-visible` keeps menus and
// popovers inside a card from being clipped. The edge is a real border (not the base ring) so
// call sites can recolour it with border-* utilities — the io drawer's blue accent depends on it.
export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <BaseCard
      className={cn("block gap-0 overflow-visible rounded-lg border py-0 ring-0", className)}
    >
      {children}
    </BaseCard>
  );
}

export function Table({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-lg border">
      <BaseTable className="border-collapse text-left">{children}</BaseTable>
    </div>
  );
}

export function Th({ children }: { children: ReactNode }) {
  return (
    <TableHead className="h-auto border-b bg-[var(--c-surface)] px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
      {children}
    </TableHead>
  );
}

export function Td({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <TableCell className={cn("whitespace-normal border-b border-border/60 px-3 py-2", className)}>
      {children}
    </TableCell>
  );
}

export function ErrorBanner({ error }: { error: unknown }) {
  if (!error) return null;
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <Alert variant="destructive" className="border-destructive/30 bg-destructive/5">
      <AlertDescription>{msg}</AlertDescription>
    </Alert>
  );
}

// The amber sibling of ErrorBanner — an informational note ("this run is paused", "durable runtime
// unavailable"), not a failure. Children go through AlertDescription as ONE child — the Alert
// surface is a grid, so bare mixed inline children (text + <span>) would each become their own
// stacked row instead of flowing as a sentence.
export function NoteBanner({ children }: { children: ReactNode }) {
  return (
    <Alert className="border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300">
      <AlertDescription className="text-inherit">{children}</AlertDescription>
    </Alert>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <BaseEmpty className="rounded-lg border border-dashed px-6 py-10">
      <EmptyDescription>{children}</EmptyDescription>
    </BaseEmpty>
  );
}

export function Spinner({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 px-3 py-8 text-sm text-muted-foreground">
      <SpinnerIcon />
      {label}
    </div>
  );
}

// A centered modal dialog (the shared overlay the bench + browse flows open into). Escape and
// backdrop click both close it (the dialog primitive owns focus trapping). `width` is a Tailwind
// max-w-* class so callers size to content.
export function Modal({
  title,
  onClose,
  children,
  width = "max-w-3xl",
}: {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  width?: string;
}) {
  // Restore focus to the opener on close. The dialog is controlled with no Trigger element, and
  // without one the primitive's close-autofocus has nothing to focus (its preventDefault also
  // suppresses the fallback), silently dropping keyboard users at <body>.
  const previouslyFocused = useRef<HTMLElement | null>(null);
  useEffect(() => {
    previouslyFocused.current = document.activeElement as HTMLElement | null;
  }, []);
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent
        // No description element exists, so opt out of the auto-wired aria-describedby — otherwise
        // every modal carries an idref that resolves to nothing.
        aria-describedby={undefined}
        onCloseAutoFocus={(e) => {
          e.preventDefault();
          previouslyFocused.current?.focus();
        }}
        // Dismiss ONLY when the pointer lands on the backdrop itself. While a modal layer is open
        // the primitive turns off pointer events on <body>; surfaces that deliberately stay
        // interactive above it (the notification toaster re-enables its own pointer events) must
        // not count as "outside" clicks that close the dialog — that was the pre-dialog behaviour.
        onPointerDownOutside={(e) => {
          const target = e.target as Element | null;
          if (!target?.closest?.('[data-slot="dialog-overlay"]')) e.preventDefault();
        }}
        // w cap keeps a 16px viewport gutter at any size; `width` (max-w-*) merges over the base
        // max-w, so without the cap a wide dialog would sit flush against a narrow viewport.
        className={cn(
          "flex max-h-[90vh] w-[calc(100%-2rem)] flex-col gap-0 overflow-hidden rounded-lg p-0 shadow-xl",
          width,
        )}
      >
        {/* pr clears the absolutely-positioned close button so long titles never run under it */}
        <DialogHeader className="shrink-0 border-b py-3 pr-12 pl-4">
          <DialogTitle className="text-sm font-semibold">{title}</DialogTitle>
        </DialogHeader>
        <div className="min-h-0 flex-1 overflow-auto p-4">{children}</div>
      </DialogContent>
    </Dialog>
  );
}

// The shared confirmation step for irreversible actions (deleting a server, a model, a credential).
// Renders as an alert dialog so every destructive flow asks the same way.
export function ConfirmDialog({
  title,
  message,
  confirmLabel = "Delete",
  onConfirm,
  onCancel,
}: {
  title: string;
  message: ReactNode;
  confirmLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  // Same opener-restore as Modal: a controlled root has no Trigger for the primitive to hand
  // focus back to.
  const previouslyFocused = useRef<HTMLElement | null>(null);
  useEffect(() => {
    previouslyFocused.current = document.activeElement as HTMLElement | null;
  }, []);
  return (
    <AlertDialog
      open
      onOpenChange={(open) => {
        if (!open) onCancel();
      }}
    >
      <AlertDialogContent
        onCloseAutoFocus={(e) => {
          e.preventDefault();
          previouslyFocused.current?.focus();
        }}
      >
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription>{message}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel onClick={onCancel}>Cancel</AlertDialogCancel>
          <AlertDialogAction variant="destructive" onClick={onConfirm}>
            {confirmLabel}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

// A thin download/progress bar. `value`/`max` in bytes; `indeterminate` animates when total unknown.
export function ProgressBar({
  value,
  max,
  indeterminate = false,
}: {
  value: number;
  max?: number | null;
  indeterminate?: boolean;
}) {
  const pct = max && max > 0 ? Math.min(100, Math.round((value / max) * 100)) : 0;
  return (
    <Progress
      value={indeterminate ? 40 : pct}
      className={cn("h-2", indeterminate && "[&_[data-slot=progress-indicator]]:animate-pulse")}
    />
  );
}
