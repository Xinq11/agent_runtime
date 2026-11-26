import React, { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Check, Copy } from "lucide-react";
import { ChartBlock, tryParseChartSpec } from "./ChartBlock";

interface MarkdownContentProps {
  content: string;
}

/**
 * Ensure GFM tables always have a blank line before the header row so remark-gfm
 * parses tables reliably even when an LLM omits spacing after a preceding paragraph.
 */
function normalizeMarkdown(raw: string): string {
  const lines = raw.replace(/\r\n/g, "\n").split("\n");
  const out: string[] = [];
  for (let i = 0; i < lines.length; i++) {
    const cur = lines[i];
    const next = lines[i + 1] || "";
    const isTableStart =
      cur.trim().startsWith("|") &&
      /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/.test(next);
    if (isTableStart && out.length > 0 && out[out.length - 1].trim() !== "") {
      out.push("");
    }
    out.push(cur);
  }
  return out.join("\n");
}

const CodeBlock: React.FC<{ language?: string; code: string }> = ({
  language,
  code,
}) => {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      // Clipboard may be unavailable on non-secure origins
    }
  };

  return (
    <div
      className="my-3 rounded-lg overflow-hidden border"
      style={{ borderColor: "var(--border-strong)", background: "var(--code-bg)" }}
    >
      <div
        className="flex items-center justify-between px-3 py-1.5 text-[11px] font-mono border-b"
        style={{
          borderColor: "rgba(255,255,255,0.08)",
          color: "#a8a298",
          background: "rgba(255,255,255,0.03)",
        }}
      >
        <span>{language || "text"}</span>
        <button
          type="button"
          onClick={handleCopy}
          className="inline-flex items-center gap-1 hover:text-white transition-colors"
        >
          {copied ? <Check size={12} /> : <Copy size={12} />}
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <pre
        className="p-3 overflow-x-auto font-mono text-[12.5px] leading-[1.6]"
        style={{ color: "var(--code-ink)" }}
      >
        <code>{code}</code>
      </pre>
    </div>
  );
};

export const MarkdownContent: React.FC<MarkdownContentProps> = ({ content }) => {
  const normalized = normalizeMarkdown(content);

  return (
    <div className="md-prose text-[14.2px] leading-[1.72] break-words">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: ({ children }) => (
            <h1 className="text-[19px] font-semibold mt-4 mb-2 tracking-tight">
              {children}
            </h1>
          ),
          h2: ({ children }) => (
            <h2 className="text-[16.5px] font-semibold mt-4 mb-2 tracking-tight">
              {children}
            </h2>
          ),
          h3: ({ children }) => (
            <h3 className="text-[15px] font-semibold mt-3 mb-1.5">{children}</h3>
          ),
          p: ({ children }) => <p className="my-2 first:mt-0 last:mb-0">{children}</p>,
          ul: ({ children }) => (
            <ul className="my-2 pl-5 list-disc space-y-1">{children}</ul>
          ),
          ol: ({ children }) => (
            <ol className="my-2 pl-5 list-decimal space-y-1">{children}</ol>
          ),
          li: ({ children }) => <li className="leading-[1.68]">{children}</li>,
          blockquote: ({ children }) => (
            <blockquote
              className="my-2.5 pl-3.5 py-0.5 border-l-2 italic"
              style={{
                borderColor: "var(--accent)",
                color: "var(--ink-secondary)",
              }}
            >
              {children}
            </blockquote>
          ),
          hr: () => (
            <hr className="my-4 border-t" style={{ borderColor: "var(--border)" }} />
          ),
          a: ({ href, children }) => (
            <a
              href={href}
              target="_blank"
              rel="noreferrer"
              className="underline underline-offset-2"
              style={{ color: "var(--accent)" }}
            >
              {children}
            </a>
          ),
          table: ({ children }) => (
            <div
              className="my-3 overflow-x-auto rounded-lg border"
              style={{ borderColor: "var(--border)", background: "var(--surface)" }}
            >
              <table className="w-full border-collapse text-[13px]">{children}</table>
            </div>
          ),
          thead: ({ children }) => (
            <thead style={{ background: "var(--surface-subtle)" }}>{children}</thead>
          ),
          th: ({ children }) => (
            <th
              className="text-left font-semibold px-3.5 py-2 border-b whitespace-nowrap"
              style={{ borderColor: "var(--border)", color: "var(--ink)" }}
            >
              {children}
            </th>
          ),
          td: ({ children }) => (
            <td
              className="px-3.5 py-2 border-b align-top"
              style={{ borderColor: "var(--border)", color: "var(--ink-secondary)" }}
            >
              {children}
            </td>
          ),
          pre: ({ children }) => <>{children}</>,
          code: ({ className, children, ...props }) => {
            const match = /language-([\w-]+)/.exec(className || "");
            const lang = match?.[1]?.toLowerCase();
            const text = String(children).replace(/\n$/, "");
            const isBlock = Boolean(lang) || text.includes("\n");

            if (isBlock && (lang === "chart" || lang === "echarts" || lang === "json")) {
              const chartSpec = tryParseChartSpec(text);
              if (chartSpec) {
                return <ChartBlock spec={chartSpec} />;
              }
            }

            if (isBlock) {
              return <CodeBlock language={lang} code={text} />;
            }
            return (
              <code
                {...props}
                className="font-mono text-[0.86em] px-1.5 py-0.5 rounded border"
                style={{
                  background: "var(--surface-subtle)",
                  borderColor: "var(--border)",
                  color: "var(--accent)",
                }}
              >
                {children}
              </code>
            );
          },
        }}
      >
        {normalized}
      </ReactMarkdown>
    </div>
  );
};
