import React, { useEffect, useRef, useState } from "react";
import { Check, ChevronDown, Copy } from "lucide-react";

interface TraceSelectorProps {
  traceId: string;
  recentTraces: string[];
  onSelectTrace: (traceId: string) => void;
}

export const TraceSelector: React.FC<TraceSelectorProps> = ({
  traceId,
  recentTraces,
  onSelectTrace,
}) => {
  const [menuOpen, setMenuOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const onDocClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [menuOpen]);

  const copyTraceId = async () => {
    try {
      await navigator.clipboard.writeText(traceId);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch {
      // ignore clipboard denial
    }
  };

  return (
    <div className="flex items-center gap-1.5 h-8">
      {recentTraces.length > 1 ? (
        <div ref={menuRef} className="relative flex-1 min-w-0 h-8">
          <button
            type="button"
            onClick={() => setMenuOpen((o) => !o)}
            className="w-full h-8 px-2.5 rounded-lg border font-mono text-[11.5px] flex items-center justify-between gap-2 text-left"
            style={{
              background: "var(--surface)",
              borderColor: menuOpen ? "var(--accent)" : "var(--border)",
              color: "var(--ink)",
            }}
          >
            <span className="truncate">
              {recentTraces[0] === traceId ? `最新 · ${traceId}` : traceId}
            </span>
            <ChevronDown
              size={13}
              className={`shrink-0 transition-transform ${menuOpen ? "rotate-180" : ""}`}
              style={{ color: "var(--ink-muted)" }}
            />
          </button>

          {menuOpen && (
            <div
              className="absolute left-0 right-0 top-[36px] z-30 rounded-xl border p-1 shadow-lg max-h-60 overflow-y-auto"
              style={{ background: "var(--surface)", borderColor: "var(--border-strong)" }}
            >
              {recentTraces.map((tr, i) => {
                const active = tr === traceId;
                return (
                  <button
                    key={tr}
                    type="button"
                    onClick={() => {
                      setMenuOpen(false);
                      onSelectTrace(tr);
                    }}
                    className="w-full flex items-center justify-between gap-2 px-2.5 py-2 rounded-lg font-mono text-[11.5px] text-left transition-colors"
                    style={{
                      background: active ? "var(--accent-soft)" : "transparent",
                      color: active ? "var(--accent)" : "var(--ink-secondary)",
                    }}
                  >
                    <span className="truncate">{i === 0 ? `最新 · ${tr}` : tr}</span>
                    {active && <Check size={12} className="shrink-0" />}
                  </button>
                );
              })}
            </div>
          )}
        </div>
      ) : (
        <div
          className="flex-1 min-w-0 h-8 flex items-center px-2.5 rounded-lg border font-mono text-[11.5px]"
          style={{ background: "var(--surface)", borderColor: "var(--border)", color: "var(--ink-secondary)" }}
        >
          <span className="truncate">{traceId}</span>
        </div>
      )}
      <button
        onClick={copyTraceId}
        title="复制 trace_id"
        className="interactive-btn h-8 w-8 flex items-center justify-center rounded-lg border shrink-0"
        style={{
          background: "var(--surface)",
          borderColor: "var(--border)",
          color: copied ? "var(--ok)" : "var(--ink-muted)",
        }}
      >
        {copied ? <Check size={13} /> : <Copy size={13} />}
      </button>
    </div>
  );
};
