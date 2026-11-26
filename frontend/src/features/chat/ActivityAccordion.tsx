import React, { useState } from "react";
import { ChevronDown, ChevronRight, Cpu, HardDrive, Terminal } from "lucide-react";
import { ActivityBlock } from "../../shared/types/runtime";

export const ActivityAccordion: React.FC<{ block: ActivityBlock }> = ({ block }) => {
  const [open, setOpen] = useState(false);
  const dotColor =
    block.phase === "started"
      ? "var(--warn)"
      : block.ok === false || block.phase === "error"
      ? "var(--danger)"
      : "var(--ok)";

  return (
    <div
      className="rounded-lg border overflow-hidden text-[12px]"
      style={{ background: "var(--surface)", borderColor: "var(--border)" }}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between gap-2 px-3 py-2 text-left"
        style={{ background: open ? "var(--surface-hover)" : "transparent" }}
      >
        <div className="flex items-center gap-2 min-w-0">
          {open ? (
            <ChevronDown size={13} style={{ color: "var(--ink-muted)" }} />
          ) : (
            <ChevronRight size={13} style={{ color: "var(--ink-muted)" }} />
          )}
          <span className="w-2 h-2 rounded-full shrink-0" style={{ background: dotColor }} />
          {block.kind === "subagent" ? (
            <Cpu size={13} style={{ color: "var(--accent)" }} />
          ) : (
            <Terminal size={13} style={{ color: "var(--ink-secondary)" }} />
          )}
          <span className="font-mono font-medium truncate">
            {block.kind === "subagent" ? `subagent:${block.name}` : block.name}
          </span>
          {block.detail && (
            <span className="truncate" style={{ color: "var(--ink-muted)" }}>
              · {block.detail}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {block.truncated && (
            <span
              className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded font-mono text-[10.5px]"
              style={{ background: "var(--warn-soft)", color: "var(--warn)" }}
            >
              <HardDrive size={10} />
              OSS ref
            </span>
          )}
          <span className="font-mono text-[10.5px]" style={{ color: "var(--ink-muted)" }}>
            {block.phase === "started" ? "running" : block.ok === false ? "failed" : "done"}
          </span>
        </div>
      </button>

      {open && (
        <div
          className="px-3 py-2.5 border-t space-y-2 font-mono text-[11.5px]"
          style={{ borderColor: "var(--border)", background: "var(--code-bg)", color: "var(--code-ink)" }}
        >
          {block.args && Object.keys(block.args).length > 0 && (
            <div>
              <div className="text-[10.5px] opacity-60 mb-1">args</div>
              <pre className="whitespace-pre-wrap break-all">{JSON.stringify(block.args, null, 2)}</pre>
            </div>
          )}
          {block.refId && (
            <div style={{ color: "#e2a44c" }}>
              ref_id: {block.refId} {block.ossKey ? `(${block.ossKey})` : ""}
            </div>
          )}
          {block.preview && (
            <div>
              <div className="text-[10.5px] opacity-60 mb-1">result</div>
              <pre className="whitespace-pre-wrap break-words opacity-90">{block.preview}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
};
