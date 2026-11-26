import { ChartSpec, sanitizeOption } from "./chartSpec";

// Warm editorial Claude Code palette (strictly zero violet/lavender)
const PALETTE = ["#D97757", "#1F7A5A", "#2F6F9F", "#C5832B", "#6D685F", "#B84A39"];

function formatNum(v: number): string {
  if (!Number.isFinite(v)) return String(v);
  if (Math.abs(v) >= 10000) return `${(v / 10000).toFixed(v % 10000 === 0 ? 0 : 1)}万`;
  return Number.isInteger(v) ? String(v) : v.toFixed(1);
}

export function readThemeTokens() {
  const cs = getComputedStyle(document.documentElement);
  const pick = (v: string, fallback: string) => cs.getPropertyValue(v).trim() || fallback;
  return {
    ink: pick("--ink", "#1e1d1a"),
    inkSecondary: pick("--ink-secondary", "#5e5b54"),
    inkMuted: pick("--ink-muted", "#878279"),
    border: pick("--border", "#e6e1d7"),
    surface: pick("--surface", "#ffffff"),
  };
}

export function buildEChartsOption(spec: ChartSpec): Record<string, unknown> {
  const theme = readThemeTokens();
  const baseTooltip = {
    backgroundColor: theme.surface,
    borderColor: theme.border,
    textStyle: { color: theme.ink, fontSize: 12 },
  };

  if (spec.type === "echarts" && spec.echartsOption) {
    const clean = sanitizeOption(spec.echartsOption) as Record<string, unknown>;
    return {
      color: PALETTE,
      backgroundColor: "transparent",
      textStyle: { color: theme.inkSecondary, fontFamily: "inherit" },
      tooltip: { trigger: "axis", ...(typeof clean.tooltip === "object" ? clean.tooltip : {}) },
      ...clean,
      title: undefined,
    };
  }

  const unit = spec.unit || "";

  if (spec.type === "pie") {
    const items = (spec.data || []).filter((d) => Number(d.value) > 0);
    return {
      color: PALETTE,
      backgroundColor: "transparent",
      tooltip: { ...baseTooltip, trigger: "item", formatter: `{b}: {c}${unit} ({d}%)` },
      legend: { orient: "vertical", right: 12, top: "middle", textStyle: { color: theme.inkSecondary, fontSize: 12 } },
      series: [
        {
          type: "pie",
          radius: ["44%", "74%"],
          center: ["36%", "50%"],
          avoidLabelOverlap: true,
          itemStyle: { borderRadius: 5, borderColor: theme.surface, borderWidth: 2 },
          label: { show: false },
          emphasis: { label: { show: true, fontSize: 12, fontWeight: 600, color: theme.ink } },
          data: items,
        },
      ],
    };
  }

  if (spec.type === "funnel") {
    const items = (spec.data || []).filter((d) => Number(d.value) >= 0);
    return {
      color: PALETTE,
      backgroundColor: "transparent",
      tooltip: { ...baseTooltip, trigger: "item", formatter: `{b}: {c}${unit}` },
      legend: { bottom: 0, textStyle: { color: theme.inkSecondary, fontSize: 11.5 } },
      series: [
        {
          type: "funnel",
          left: "12%",
          right: "12%",
          top: 16,
          bottom: 36,
          sort: "descending",
          gap: 3,
          label: { show: true, position: "inside", color: "#fff", fontSize: 11.5 },
          data: items,
        },
      ],
    };
  }

  if (spec.type === "radar") {
    return {
      color: PALETTE,
      backgroundColor: "transparent",
      tooltip: baseTooltip,
      legend: { bottom: 0, textStyle: { color: theme.inkSecondary, fontSize: 11.5 } },
      radar: {
        radius: "64%",
        center: ["50%", "46%"],
        indicator: (spec.indicators || []).map((ind) => ({
          name: ind.name,
          ...(ind.max !== undefined ? { max: ind.max } : {}),
        })),
        axisName: { color: theme.inkSecondary, fontSize: 11.5 },
        splitLine: { lineStyle: { color: theme.border } },
        splitArea: { show: false },
        axisLine: { lineStyle: { color: theme.border } },
      },
      series: [
        {
          type: "radar",
          data: (spec.series || []).map((s) => ({
            name: s.name,
            value: s.data,
            areaStyle: { opacity: 0.14 },
          })),
        },
      ],
    };
  }

  if (spec.type === "scatter") {
    return {
      color: PALETTE,
      backgroundColor: "transparent",
      tooltip: { ...baseTooltip, trigger: "item" },
      legend: { top: 0, right: 4, textStyle: { color: theme.inkSecondary, fontSize: 11.5 } },
      grid: { left: 48, right: 20, top: 32, bottom: 28 },
      xAxis: {
        type: "value",
        axisLabel: { color: theme.inkMuted, fontSize: 11 },
        splitLine: { lineStyle: { color: theme.border, type: "dashed" } },
      },
      yAxis: {
        type: "value",
        axisLabel: { color: theme.inkMuted, fontSize: 11, formatter: (v: number) => formatNum(v) },
        splitLine: { lineStyle: { color: theme.border, type: "dashed" } },
      },
      series: (spec.series || []).map((s) => ({
        name: s.name,
        type: "scatter",
        symbolSize: 10,
        data: s.data,
      })),
    };
  }

  const labels = spec.x || [];
  const rawSeries = spec.series || [];
  const hasDualAxis = spec.type === "combo" && rawSeries.some((s) => (s.yAxisIndex ?? 0) === 1);

  const yAxes: Record<string, unknown>[] = [
    {
      type: "value",
      name: unit || undefined,
      nameTextStyle: { color: theme.inkMuted, fontSize: 10.5 },
      axisLabel: { color: theme.inkMuted, fontSize: 11, formatter: (v: number) => formatNum(v) },
      splitLine: { lineStyle: { color: theme.border, type: "dashed" } },
    },
  ];
  if (hasDualAxis) {
    yAxes.push({
      type: "value",
      name: spec.unitRight || undefined,
      nameTextStyle: { color: theme.inkMuted, fontSize: 10.5 },
      axisLabel: { color: theme.inkMuted, fontSize: 11, formatter: (v: number) => formatNum(v) },
      splitLine: { show: false },
    });
  }

  const builtSeries = rawSeries.map((s) => {
    const resolvedType =
      spec.type === "combo"
        ? s.type === "area"
          ? "line"
          : s.type || "bar"
        : spec.type === "stacked_bar"
        ? "bar"
        : spec.type === "area"
        ? "line"
        : spec.type;
    const isArea = spec.type === "area" || s.type === "area";
    const isStacked = spec.type === "stacked_bar" || Boolean(s.stack);

    return {
      name: s.name,
      type: resolvedType,
      yAxisIndex: hasDualAxis ? s.yAxisIndex ?? 0 : 0,
      stack: isStacked ? s.stack || "total" : undefined,
      smooth: resolvedType === "line" ? 0.25 : undefined,
      symbolSize: resolvedType === "line" ? 6 : undefined,
      barMaxWidth: resolvedType === "bar" ? 38 : undefined,
      itemStyle:
        resolvedType === "bar" ? { borderRadius: isStacked ? [0, 0, 0, 0] : [4, 4, 0, 0] } : undefined,
      areaStyle: isArea ? { opacity: 0.16 } : undefined,
      label:
        resolvedType === "bar" && !isStacked && rawSeries.length <= 2
          ? {
              show: true,
              position: "top",
              color: theme.inkSecondary,
              fontSize: 10.5,
              formatter: (p: { value: number }) => formatNum(Number(p.value)),
            }
          : undefined,
      data: s.data,
    };
  });

  return {
    color: PALETTE,
    backgroundColor: "transparent",
    tooltip: {
      ...baseTooltip,
      trigger: "axis",
      axisPointer: { type: spec.type === "bar" || spec.type === "stacked_bar" ? "shadow" : "line" },
    },
    legend:
      rawSeries.length > 1
        ? { top: 0, right: 4, textStyle: { color: theme.inkSecondary, fontSize: 11.5 } }
        : undefined,
    grid: {
      left: 14,
      right: hasDualAxis ? 18 : 14,
      top: rawSeries.length > 1 || unit ? 34 : 22,
      bottom: 8,
      containLabel: true,
    },
    xAxis: {
      type: "category",
      data: labels,
      axisLine: { lineStyle: { color: theme.border } },
      axisTick: { show: false },
      axisLabel: { color: theme.inkMuted, fontSize: 11 },
    },
    yAxis: hasDualAxis ? yAxes : yAxes[0],
    series: builtSeries,
  };
}
