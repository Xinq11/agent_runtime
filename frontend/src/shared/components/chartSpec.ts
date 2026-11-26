export interface ChartSeries {
  name: string;
  data: Array<number | [number, number]>;
  type?: "bar" | "line" | "area" | "scatter";
  yAxisIndex?: number;
  stack?: string;
}

export interface ChartSpec {
  type:
    | "bar"
    | "stacked_bar"
    | "line"
    | "area"
    | "pie"
    | "scatter"
    | "radar"
    | "funnel"
    | "combo"
    | "echarts";
  title?: string;
  unit?: string;
  unitRight?: string;
  x?: string[];
  labels?: string[];
  indicators?: Array<{ name: string; max?: number }>;
  series?: ChartSeries[];
  data?: Array<{ name: string; value: number }>;
  echartsOption?: Record<string, unknown>;
}

const SUPPORTED_TYPES = new Set([
  "bar",
  "stacked_bar",
  "line",
  "area",
  "pie",
  "scatter",
  "radar",
  "funnel",
  "combo",
  "echarts",
]);

/**
 * Strip unsafe keys / stringified JS functions from user/LLM-supplied ECharts options.
 */
export function sanitizeOption(value: unknown, depth = 0): unknown {
  if (depth > 10 || value === null || value === undefined) return value;
  if (typeof value === "number" || typeof value === "boolean") return value;
  if (typeof value === "string") return value.replace(/<\s*script/gi, "");
  if (Array.isArray(value)) return value.map((item) => sanitizeOption(item, depth + 1));
  if (typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      if (k === "__proto__" || k === "constructor" || k === "prototype") continue;
      if (typeof v === "function") continue;
      out[k] = sanitizeOption(v, depth + 1);
    }
    return out;
  }
  return undefined;
}

export function tryParseChartSpec(raw: string): ChartSpec | null {
  try {
    const obj = JSON.parse(raw);
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) return null;

    if (obj.echartsOption && typeof obj.echartsOption === "object") {
      return {
        type: "echarts",
        title: typeof obj.title === "string" ? obj.title : undefined,
        unit: typeof obj.unit === "string" ? obj.unit : undefined,
        echartsOption: obj.echartsOption as Record<string, unknown>,
      };
    }
    if (!obj.type && (Array.isArray(obj.series) || obj.xAxis || obj.radar)) {
      const titleText =
        typeof obj.title === "string"
          ? obj.title
          : typeof obj.title?.text === "string"
          ? obj.title.text
          : undefined;
      return { type: "echarts", title: titleText, echartsOption: obj as Record<string, unknown> };
    }

    const t = String(obj.type || "").toLowerCase();
    if (!SUPPORTED_TYPES.has(t)) return null;

    if (t === "pie" || t === "funnel") {
      if (Array.isArray(obj.data) && obj.data.length > 0) {
        return { ...obj, type: t as ChartSpec["type"] };
      }
      if (Array.isArray(obj.x) && Array.isArray(obj.series) && obj.series[0]?.data) {
        return {
          ...obj,
          type: t as ChartSpec["type"],
          data: obj.x.map((name: string, i: number) => ({
            name: String(name),
            value: Number(obj.series[0].data[i] ?? 0),
          })),
        };
      }
      return null;
    }

    if (t === "radar") {
      return Array.isArray(obj.indicators) && Array.isArray(obj.series) && obj.series.length > 0
        ? { ...obj, type: "radar" }
        : null;
    }

    if (t === "scatter") {
      return Array.isArray(obj.series) && obj.series.length > 0 ? { ...obj, type: "scatter" } : null;
    }

    const xAxis = Array.isArray(obj.x) ? obj.x : Array.isArray(obj.labels) ? obj.labels : null;
    if (!xAxis || xAxis.length === 0) return null;

    if (Array.isArray(obj.series) && obj.series.length > 0) {
      return { ...obj, type: t as ChartSpec["type"], x: xAxis.map(String) };
    }
    if (Array.isArray(obj.data) && obj.data.length > 0 && typeof obj.data[0] === "number") {
      return {
        ...obj,
        type: t as ChartSpec["type"],
        x: xAxis.map(String),
        series: [{ name: obj.title || "数值", data: obj.data.map(Number) }],
      };
    }
    return null;
  } catch {
    return null;
  }
}
