import React, { useEffect, useRef, useState } from "react";
import { ArrowUp, FileText, Loader2, Paperclip, Square, X } from "lucide-react";
import { ModelConfig, WorkspaceFileSummary } from "../../shared/types/runtime";

interface ComposerProps {
  loading: boolean;
  focusNonce?: number;
  modelConfig: ModelConfig;
  onSend: (text: string) => void;
  onAbort?: () => void;
  onUploadFile?: (file: File) => Promise<WorkspaceFileSummary>;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export const Composer: React.FC<ComposerProps> = ({
  loading,
  focusNonce,
  modelConfig,
  onSend,
  onAbort,
  onUploadFile,
}) => {
  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<WorkspaceFileSummary[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (focusNonce) {
      textareaRef.current?.focus();
    }
  }, [focusNonce]);

  const handleFilesSelected = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    e.target.value = "";
    if (!files.length || !onUploadFile) return;
    setUploading(true);
    setUploadError("");
    try {
      for (const file of files) {
        const uploaded = await onUploadFile(file);
        setAttachments((prev) => {
          const path = uploaded.logical_path || uploaded.path || file.name;
          const filtered = prev.filter((a) => (a.logical_path || a.path) !== path);
          return [...filtered, uploaded];
        });
      }
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "上传附件失败");
    } finally {
      setUploading(false);
    }
  };

  const submit = () => {
    const trimmed = input.trim();
    if ((!trimmed && attachments.length === 0) || loading || uploading) return;
    const paths = attachments.map((a) => a.logical_path || a.path || "").filter(Boolean);
    const attachmentNote =
      paths.length > 0 ? `[已上传工作区附件: ${paths.join(", ")}]` : "";
    const finalPrompt = trimmed
      ? attachmentNote
        ? `${trimmed}\n\n${attachmentNote}`
        : trimmed
      : `请读取并分析我刚上传的附件。\n\n${attachmentNote}`;
    setInput("");
    setAttachments([]);
    setUploadError("");
    onSend(finalPrompt);
  };

  return (
    <div className="px-5 pb-4 pt-2 shrink-0">
      <div
        className="max-w-3xl mx-auto rounded-2xl border p-2.5 shadow-sm"
        style={{ background: "var(--surface)", borderColor: "var(--border-strong)" }}
      >
        {attachments.length > 0 && (
          <div className="flex flex-wrap gap-1.5 px-1.5 pb-2">
            {attachments.map((att) => {
              const p = att.logical_path || att.path || "";
              const name = p.split("/").pop() || p;
              return (
                <span
                  key={p}
                  className="inline-flex items-center gap-1.5 px-2 py-1 rounded-lg border text-[11.5px]"
                  style={{
                    background: "var(--bg)",
                    borderColor: "var(--border)",
                    color: "var(--ink-secondary)",
                  }}
                >
                  <FileText size={12} style={{ color: "var(--accent)" }} />
                  <span className="font-mono max-w-[180px] truncate">{name}</span>
                  <span className="text-[10.5px]" style={{ color: "var(--ink-muted)" }}>
                    {formatBytes(att.size_bytes)}
                  </span>
                  <button
                    type="button"
                    aria-label={`移除附件 ${name}`}
                    onClick={() =>
                      setAttachments((prev) => prev.filter((x) => (x.logical_path || x.path) !== p))
                    }
                    className="p-0.5 rounded hover:opacity-80"
                    style={{ color: "var(--ink-muted)" }}
                  >
                    <X size={11} />
                  </button>
                </span>
              );
            })}
          </div>
        )}

        {uploadError && (
          <div
            className="mx-1.5 mb-2 px-2.5 py-1.5 rounded-lg text-[11.5px] flex items-center justify-between gap-2"
            style={{ background: "var(--danger-soft)", color: "var(--danger)" }}
          >
            <span className="truncate">{uploadError}</span>
            <button
              type="button"
              onClick={() => setUploadError("")}
              aria-label="关闭上传错误提示"
              className="shrink-0"
            >
              <X size={12} />
            </button>
          </div>
        )}

        <textarea
          ref={textareaRef}
          rows={2}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          placeholder="输入问题，Enter 发送 · Shift+Enter 换行..."
          className="w-full resize-none bg-transparent px-2 py-1 text-[14px] outline-none leading-relaxed"
          style={{ color: "var(--ink)" }}
        />

        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept=".md,.markdown,.txt,.csv,.tsv,.json,.yaml,.yml,.log,.sql,.pdf,.docx,.xlsx"
          onChange={handleFilesSelected}
          className="hidden"
        />

        <div className="flex items-center justify-between pt-1.5 px-1 gap-2">
          <div className="flex items-center gap-2 min-w-0">
            {onUploadFile && (
              <button
                type="button"
                disabled={uploading || loading}
                onClick={() => fileInputRef.current?.click()}
                title="上传附件（支持文本/CSV/JSON/Markdown 与 PDF/Word/Excel，≤10MB）"
                className="interactive-btn inline-flex items-center gap-1 px-2 py-1 rounded-lg border text-[11.5px] shrink-0 disabled:opacity-40"
                style={{
                  background: "var(--bg)",
                  borderColor: "var(--border)",
                  color: "var(--ink-secondary)",
                }}
              >
                {uploading ? <Loader2 size={13} className="animate-spin" /> : <Paperclip size={13} />}
                {uploading ? "解析中..." : "上传附件"}
              </button>
            )}
            <span className="text-[11.5px] font-mono truncate" style={{ color: "var(--ink-muted)" }}>
              {modelConfig.has_api_key ? modelConfig.model : "未配置 DeepSeek API Key"} ·
              支持表格、图表与文档解析
            </span>
          </div>

          {loading ? (
            <button
              type="button"
              onClick={onAbort}
              title="停止生成"
              aria-label="停止生成"
              className="interactive-btn w-8 h-8 rounded-lg flex items-center justify-center text-white shrink-0"
              style={{ background: "var(--danger)" }}
            >
              <Square size={13} fill="currentColor" />
            </button>
          ) : (
            <button
              type="button"
              onClick={submit}
              disabled={(!input.trim() && attachments.length === 0) || uploading}
              aria-label="发送消息"
              className="interactive-btn w-8 h-8 rounded-lg flex items-center justify-center text-white disabled:opacity-40 shrink-0"
              style={{ background: "var(--accent)" }}
            >
              <ArrowUp size={16} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
};
