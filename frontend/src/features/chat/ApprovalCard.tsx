import React from "react";
import { Check, ShieldAlert, X } from "lucide-react";
import { ApprovalTicket, ChatTurnItem } from "../../shared/types/runtime";

interface ApprovalCardProps {
  itemId: string;
  ticket: ApprovalTicket;
  status?: ChatTurnItem["approvalStatus"];
  onDecision: (itemId: string, ticket: ApprovalTicket, approved: boolean) => void;
}

export const ApprovalCard: React.FC<ApprovalCardProps> = ({
  itemId,
  ticket,
  status,
  onDecision,
}) => {
  return (
    <div
      className="rounded-xl border p-4 space-y-3"
      style={{ background: "var(--warn-soft)", borderColor: "var(--warn)" }}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2 font-semibold text-[13.5px]">
          <ShieldAlert size={16} style={{ color: "var(--warn)" }} />
          <span>高危写操作已触发 LangGraph interrupt()，等待人工审批</span>
        </div>
        <span
          className="font-mono text-[11px] px-2 py-0.5 rounded"
          style={{ background: "var(--surface)", color: "var(--ink-secondary)" }}
        >
          {ticket.tool_name}
        </span>
      </div>

      <pre
        className="rounded-lg p-3 font-mono text-[11.8px] overflow-x-auto"
        style={{ background: "var(--code-bg)", color: "var(--code-ink)" }}
      >
        {JSON.stringify(ticket.summary, null, 2)}
      </pre>

      <div className="flex items-center justify-between gap-3 flex-wrap">
        <span className="font-mono text-[11px]" style={{ color: "var(--ink-muted)" }}>
          review_id: {ticket.review_id}
        </span>

        {status === "approved" ? (
          <span className="inline-flex items-center gap-1 text-[12.5px] font-semibold" style={{ color: "var(--ok)" }}>
            <Check size={14} /> 已同意并消费审批票据
          </span>
        ) : status === "rejected" ? (
          <span
            className="inline-flex items-center gap-1 text-[12.5px] font-semibold"
            style={{ color: "var(--danger)" }}
          >
            <X size={14} /> 已拒绝执行
          </span>
        ) : (
          <div className="flex items-center gap-2">
            <button
              disabled={status === "submitting"}
              onClick={() => onDecision(itemId, ticket, false)}
              className="interactive-btn inline-flex items-center gap-1 px-3 py-1.5 rounded-lg border text-[12.5px] font-medium"
              style={{ background: "var(--surface)", borderColor: "var(--border-strong)", color: "var(--danger)" }}
            >
              <X size={13} /> 拒绝
            </button>
            <button
              disabled={status === "submitting"}
              onClick={() => onDecision(itemId, ticket, true)}
              className="interactive-btn inline-flex items-center gap-1 px-3.5 py-1.5 rounded-lg text-[12.5px] font-medium text-white"
              style={{ background: "var(--ok)" }}
            >
              <Check size={13} />
              {status === "submitting" ? "恢复线程中..." : "同意并恢复执行"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
};
