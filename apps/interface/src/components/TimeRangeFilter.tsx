// A time-window picker for list pages, in the spirit of a dashboard's time control: a compact
// trigger showing the active window, opening onto quick rolling ranges (last 5m … last 90d) beside
// an absolute from/to range. The page owns the TimeRange and does the filtering (see lib/timeRange);
// this is only the control. Meant to live in a FilterBar's trailing cluster.

import { Clock, X } from "lucide-react";
import { useEffect, useId, useState } from "react";
import {
  ALL_TIME,
  QUICK_RANGES,
  type TimeRange,
  fromLocalInput,
  isActive,
  rangeLabel,
  toLocalInput,
} from "../lib/timeRange";
import { cn } from "../lib/utils";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "./ui/popover";

export function TimeRangeFilter({
  value,
  onChange,
}: {
  value: TimeRange;
  onChange: (r: TimeRange) => void;
}) {
  const [open, setOpen] = useState(false);
  const active = isActive(value);
  const errorId = useId();

  // The absolute inputs are local drafts (datetime-local wall-clock strings) applied on demand, so
  // typing a half-finished date doesn't filter mid-keystroke. Re-seed them from the current window
  // each time the picker opens.
  const [fromStr, setFromStr] = useState("");
  const [toStr, setToStr] = useState("");
  useEffect(() => {
    if (!open) return;
    if (value.type === "absolute") {
      setFromStr(value.fromMs != null ? toLocalInput(value.fromMs) : "");
      setToStr(value.toMs != null ? toLocalInput(value.toMs) : "");
    } else {
      setFromStr("");
      setToStr("");
    }
  }, [open, value]);

  const fromMs = fromLocalInput(fromStr);
  const toMs = fromLocalInput(toStr);
  const invalid = fromMs != null && toMs != null && fromMs > toMs;
  const canApply = (fromMs != null || toMs != null) && !invalid;

  const applyAbsolute = () => {
    if (!canApply) return;
    onChange({ type: "absolute", fromMs, toMs });
    setOpen(false);
  };

  return (
    <div className="flex items-center gap-1">
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button
            variant="outline"
            size="sm"
            className={cn(
              "gap-1.5",
              active &&
                "border-primary/50 bg-primary/10 text-primary hover:bg-primary/10 hover:text-primary aria-expanded:bg-primary/10 aria-expanded:text-primary",
            )}
            title="Filter by time"
          >
            <Clock size={14} />
            <span className="max-w-[13rem] truncate">{rangeLabel(value)}</span>
          </Button>
        </PopoverTrigger>
        <PopoverContent align="end" className="w-[440px] gap-0 p-0">
          <div className="flex divide-x divide-border">
            <div className="flex w-40 shrink-0 flex-col">
              <p className="px-2.5 pt-2.5 pb-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                Quick ranges
              </p>
              <div className="max-h-72 overflow-y-auto p-1.5 pt-0">
                {QUICK_RANGES.map((q) => {
                  const selected = value.type === "relative" && value.ms === q.ms;
                  return (
                    <button
                      key={q.ms}
                      type="button"
                      onClick={() => {
                        onChange({ type: "relative", ms: q.ms });
                        setOpen(false);
                      }}
                      className={cn(
                        "w-full rounded-md px-2 py-1 text-left text-xs transition-colors",
                        selected
                          ? "bg-primary/10 font-medium text-primary"
                          : "text-foreground hover:bg-muted",
                      )}
                    >
                      {q.label}
                    </button>
                  );
                })}
              </div>
            </div>

            <div className="flex min-w-0 flex-1 flex-col gap-2 p-2.5">
              <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                Absolute range
              </p>
              <label className="flex flex-col gap-1 text-[11px] text-muted-foreground">
                From
                <Input
                  type="datetime-local"
                  value={fromStr}
                  onChange={(e) => setFromStr(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") applyAbsolute();
                  }}
                  aria-invalid={invalid}
                  aria-describedby={invalid ? errorId : undefined}
                  className="text-xs"
                />
              </label>
              <label className="flex flex-col gap-1 text-[11px] text-muted-foreground">
                To
                <Input
                  type="datetime-local"
                  value={toStr}
                  onChange={(e) => setToStr(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") applyAbsolute();
                  }}
                  aria-invalid={invalid}
                  aria-describedby={invalid ? errorId : undefined}
                  className="text-xs"
                />
              </label>
              {invalid && (
                <p id={errorId} role="alert" className="text-[11px] text-destructive">
                  “From” is after “To”.
                </p>
              )}
              <Button size="sm" disabled={!canApply} onClick={applyAbsolute} className="mt-0.5">
                Apply range
              </Button>
            </div>
          </div>

          <div className="flex items-center justify-between border-t border-border px-2.5 py-1.5">
            <span className="text-[11px] text-muted-foreground">
              Leave “To” empty for “until now”.
            </span>
            <button
              type="button"
              disabled={!active}
              onClick={() => {
                onChange(ALL_TIME);
                setOpen(false);
              }}
              className="text-xs text-muted-foreground hover:text-foreground disabled:opacity-40"
            >
              All time
            </button>
          </div>
        </PopoverContent>
      </Popover>

      {active && (
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Clear time filter"
          title="Clear time filter"
          onClick={() => onChange(ALL_TIME)}
        >
          <X size={14} />
        </Button>
      )}
    </div>
  );
}
