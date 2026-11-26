import { SpanRecord } from "../../shared/types/runtime";

export interface GapSegment {
  startMs: number;
  endMs: number;
}

export interface WaterfallRow {
  sp: SpanRecord;
  depth: number;
  sharePct: number;
  shareLabel: string;
  isSubOfParent: boolean;
}

export interface WaterfallGapRow {
  durationMs: number;
  sharePct: number;
  shareLabel: string;
  segments: GapSegment[];
}

export interface WaterfallModel {
  minStart: number;
  totalMs: number;
  llmCount: number;
  toolCount: number;
  overall: "idle" | "ok" | "paused" | "error";
  rootSpan: SpanRecord | null;
  rows: WaterfallRow[];
  gapRow: WaterfallGapRow | null;
  shareBySpanId: Map<string, string>;
}

export function formatMs(ms: number): string {
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`;
  return `${ms}ms`;
}

/**
 * Largest-remainder (Hamilton) allocation in tenths of a percent (sum === 1000 => 100.0%).
 */
export function allocatePercentagesTenths(weights: number[]): number[] {
  const safe = weights.map((w) => (Number.isFinite(w) && w > 0 ? w : 0));
  const total = safe.reduce((acc, v) => acc + v, 0);
  if (total <= 0) return safe.map(() => 0);

  const exact = safe.map((w) => (w / total) * 1000);
  const floors = exact.map((v) => Math.floor(v));
  let remainder = 1000 - floors.reduce((acc, v) => acc + v, 0);

  const order = exact
    .map((v, idx) => ({ idx, frac: v - floors[idx], weight: safe[idx] }))
    .sort((a, b) => (b.frac !== a.frac ? b.frac - a.frac : b.weight - a.weight));

  for (let i = 0; i < order.length && remainder > 0; i++) {
    floors[order[i].idx] += 1;
    remainder -= 1;
  }
  return floors;
}

export function computeTimelineGaps(
  minStart: number,
  maxEnd: number,
  spans: SpanRecord[]
): { gapMs: number; segments: GapSegment[]; coveredMs: number } {
  const intervals = spans
    .map((s) => ({
      start: Math.max(minStart, s.start_ms),
      end: Math.min(maxEnd, s.start_ms + Math.max(0, s.duration_ms)),
    }))
    .filter((iv) => iv.end > iv.start)
    .sort((a, b) => a.start - b.start);

  const merged: GapSegment[] = [];
  for (const iv of intervals) {
    if (!merged.length || iv.start > merged[merged.length - 1].endMs) {
      merged.push({ startMs: iv.start, endMs: iv.end });
    } else if (iv.end > merged[merged.length - 1].endMs) {
      merged[merged.length - 1].endMs = iv.end;
    }
  }

  const segments: GapSegment[] = [];
  let cursor = minStart;
  let coveredMs = 0;
  for (const m of merged) {
    if (m.startMs > cursor) {
      segments.push({ startMs: cursor, endMs: m.startMs });
    }
    coveredMs += m.endMs - m.startMs;
    cursor = Math.max(cursor, m.endMs);
  }
  if (maxEnd > cursor) {
    segments.push({ startMs: cursor, endMs: maxEnd });
  }
  return { gapMs: Math.max(0, maxEnd - minStart - coveredMs), segments, coveredMs };
}

export function compactAttrSummary(attrs: Record<string, unknown>): string[] {
  const pills: string[] = [];
  if (typeof attrs.input_tokens_est === "number") pills.push(`in≈${attrs.input_tokens_est} tok`);
  if (typeof attrs.kept_messages === "number") pills.push(`msgs:${attrs.kept_messages}`);
  if (typeof attrs.model === "string") pills.push(attrs.model);
  if (Array.isArray(attrs.tool_calls) && attrs.tool_calls.length > 0) {
    pills.push(`calls:${attrs.tool_calls.join(",")}`);
  }
  if (attrs.truncated) pills.push("OSS truncated");
  if (attrs.compacted) pills.push("compacted");
  if (typeof attrs.error_code === "string" && attrs.error_code) pills.push(`err:${attrs.error_code}`);
  return pills.slice(0, 3);
}

export function buildWaterfallModel(spans: SpanRecord[]): WaterfallModel {
  if (!spans.length) {
    return {
      minStart: 0,
      totalMs: 0,
      llmCount: 0,
      toolCount: 0,
      overall: "idle",
      rootSpan: null,
      rows: [],
      gapRow: null,
      shareBySpanId: new Map<string, string>(),
    };
  }

  const minStart = Math.min(...spans.map((s) => s.start_ms));
  const maxEnd = Math.max(...spans.map((s) => s.start_ms + Math.max(1, s.duration_ms)));
  const totalMs = Math.max(1, maxEnd - minStart);
  const llmCount = spans.filter((s) => s.operation_name.startsWith("model")).length;
  const toolCount = spans.filter(
    (s) => s.operation_name.startsWith("tool:") || s.operation_name.startsWith("subagent:")
  ).length;
  const overall = spans.some((s) => s.status === "error")
    ? "error"
    : spans.some((s) => s.status === "paused")
    ? "paused"
    : "ok";

  const rootCandidates = spans.filter((s) => !s.parent_span_id);
  const rootSpan = rootCandidates.length === 1 && spans.length > 1 ? rootCandidates[0] : null;
  const stageSpans = rootSpan ? spans.filter((s) => s.span_id !== rootSpan.span_id) : spans;

  const stageById = new Map(stageSpans.map((s) => [s.span_id, s]));
  const childrenByParent = new Map<string, SpanRecord[]>();
  const topStages: SpanRecord[] = [];

  for (const sp of stageSpans) {
    if (sp.parent_span_id && stageById.has(sp.parent_span_id)) {
      const list = childrenByParent.get(sp.parent_span_id) || [];
      list.push(sp);
      childrenByParent.set(sp.parent_span_id, list);
    } else {
      topStages.push(sp);
    }
  }

  const { gapMs, segments: gapSegments } = computeTimelineGaps(minStart, maxEnd, topStages);
  const includeGap = gapMs > 0;
  const weights = topStages.map((s) => Math.max(0, s.duration_ms));
  if (includeGap) weights.push(gapMs);
  const tenths = allocatePercentagesTenths(weights);

  const shareBySpanId = new Map<string, string>();
  if (rootSpan) shareBySpanId.set(rootSpan.span_id, "100.0%");

  const rows: WaterfallRow[] = [];
  const appendChildren = (parent: SpanRecord, depth: number) => {
    const kids = childrenByParent.get(parent.span_id) || [];
    const parentDur = Math.max(1, parent.duration_ms);
    for (const kid of kids) {
      const pctOfParent = Math.min(100, (kid.duration_ms / parentDur) * 100);
      const label = `占父级 ${pctOfParent.toFixed(1)}%`;
      shareBySpanId.set(kid.span_id, label);
      rows.push({ sp: kid, depth, sharePct: pctOfParent, shareLabel: label, isSubOfParent: true });
      appendChildren(kid, depth + 1);
    }
  };

  topStages.forEach((sp, idx) => {
    const pct = tenths[idx] / 10;
    const label = `${pct.toFixed(1)}%`;
    shareBySpanId.set(sp.span_id, label);
    rows.push({ sp, depth: 0, sharePct: pct, shareLabel: label, isSubOfParent: false });
    appendChildren(sp, 1);
  });

  const gapRow = includeGap
    ? {
        durationMs: gapMs,
        sharePct: tenths[topStages.length] / 10,
        shareLabel: `${(tenths[topStages.length] / 10).toFixed(1)}%`,
        segments: gapSegments,
      }
    : null;

  return { minStart, totalMs, llmCount, toolCount, overall, rootSpan, rows, gapRow, shareBySpanId };
}
