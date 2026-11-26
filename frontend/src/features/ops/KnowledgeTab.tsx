import React, { useRef, useState } from "react";
import { CheckCircle2, FileText, Power, Upload, X } from "lucide-react";
import { updateKnowledgeStatus, uploadKnowledgeDoc } from "../../shared/api/client";
import { OpsOverview } from "../../shared/types/runtime";

interface KnowledgeTabProps {
  chunks: OpsOverview["knowledge_chunks"];
  onRefresh: () => Promise<void>;
}

function stripExt(filename: string): string {
  return filename.replace(/\.[^.]+$/, "");
}

export const KnowledgeTab: React.FC<KnowledgeTabProps> = ({ chunks, onRefresh }) => {
  const [title, setTitle] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [statusMsg, setStatusMsg] = useState("");
  const [isError, setIsError] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [togglingId, setTogglingId] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleFilePick = (picked: File | null) => {
    setFile(picked);
    if (picked) {
      setTitle((prev) => prev.trim() || stripExt(picked.name));
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file) return;
    const resolvedTitle = title.trim() || stripExt(file.name);

    setUploading(true);
    setStatusMsg("");
    setIsError(false);
    try {
      const res = await uploadKnowledgeDoc({ title: resolvedTitle, file });
      setStatusMsg(`上传成功，已建立 ${res.chunks_indexed} 个检索切片`);
      setTitle("");
      setFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      await onRefresh();
    } catch (err) {
      setIsError(true);
      setStatusMsg(err instanceof Error ? err.message : "上传失败");
    } finally {
      setUploading(false);
    }
  };

  const handleToggleStatus = async (parentId: string, currentStatus: "active" | "disabled") => {
    const next = currentStatus === "active" ? "disabled" : "active";
    setTogglingId(parentId);
    try {
      await updateKnowledgeStatus(parentId, next);
      await onRefresh();
    } finally {
      setTogglingId(null);
    }
  };

  const activeCount = chunks.filter((c) => (c.status || "active") === "active").length;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-12 gap-5">
      <form
        onSubmit={handleSubmit}
        className="lg:col-span-4 rounded-xl border p-4 space-y-3.5 h-fit"
        style={{ background: "var(--surface)", borderColor: "var(--border)" }}
      >
        <div className="flex items-center gap-2 font-semibold text-[13.5px]">
          <Upload size={15} style={{ color: "var(--accent)" }} />
          上传知识文档
        </div>

        <input
          ref={fileInputRef}
          type="file"
          accept=".md,.txt,.csv,.json,.yaml,.yml,.log,text/plain,text/markdown"
          onChange={(e) => handleFilePick(e.target.files?.[0] || null)}
          className="hidden"
        />

        {file ? (
          <div
            className="flex items-center justify-between gap-2 px-3 py-3 rounded-xl border text-[12.5px]"
            style={{ background: "var(--accent-soft)", borderColor: "var(--accent-border)" }}
          >
            <div className="flex items-center gap-2 min-w-0">
              <FileText size={15} className="shrink-0" style={{ color: "var(--accent)" }} />
              <div className="min-w-0">
                <div className="font-mono font-medium truncate">{file.name}</div>
                <div className="font-mono text-[11px]" style={{ color: "var(--ink-muted)" }}>
                  {(file.size / 1024).toFixed(1)} KB
                </div>
              </div>
            </div>
            <button
              type="button"
              onClick={() => {
                setFile(null);
                if (fileInputRef.current) fileInputRef.current.value = "";
              }}
              aria-label="移除文件"
              className="p-1 rounded-md hover:bg-black/5"
              style={{ color: "var(--ink-muted)" }}
            >
              <X size={14} />
            </button>
          </div>
        ) : (
          <div
            role="button"
            tabIndex={0}
            onClick={() => fileInputRef.current?.click()}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") fileInputRef.current?.click();
            }}
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              handleFilePick(e.dataTransfer.files?.[0] || null);
            }}
            className="interactive-btn w-full py-7 px-4 rounded-xl border border-dashed text-center space-y-1.5 cursor-pointer"
            style={{
              background: dragging ? "var(--accent-soft)" : "var(--bg)",
              borderColor: dragging ? "var(--accent)" : "var(--border-strong)",
            }}
          >
            <Upload size={18} className="mx-auto" style={{ color: "var(--accent)" }} />
            <div className="text-[13px] font-medium">点击或拖拽文件到此处上传</div>
            <div className="text-[11.5px]" style={{ color: "var(--ink-muted)" }}>
              支持 .md / .txt / .csv / .json（UTF-8，最大 2 MiB）
            </div>
          </div>
        )}

        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="文档名称（选文件后自动填入，可修改）"
          className="w-full px-3 py-2 rounded-lg border text-[13px] outline-none"
          style={{ background: "var(--bg)", borderColor: "var(--border-strong)", color: "var(--ink)" }}
        />

        <button
          type="submit"
          disabled={uploading || !file}
          className="interactive-btn w-full py-2.5 rounded-lg font-medium text-white text-[13px] disabled:opacity-40"
          style={{ background: "var(--accent)" }}
        >
          {uploading ? "上传并建索引中..." : "上传并生效"}
        </button>

        {statusMsg && (
          <div className="font-mono text-[11.5px]" style={{ color: isError ? "var(--danger)" : "var(--ok)" }}>
            {statusMsg}
          </div>
        )}
      </form>

      <div
        className="lg:col-span-8 rounded-xl border overflow-hidden h-fit"
        style={{ background: "var(--surface)", borderColor: "var(--border)" }}
      >
        <div
          className="px-4 py-3 border-b flex items-center justify-between gap-2"
          style={{ borderColor: "var(--border)" }}
        >
          <span className="font-semibold text-[13.5px]">已上传知识文档 ({chunks.length})</span>
          <span className="font-mono text-[11.5px]" style={{ color: "var(--ink-muted)" }}>
            生效中 {activeCount} · 已下架 {chunks.length - activeCount}
          </span>
        </div>

        <div className="divide-y" style={{ borderColor: "var(--border)" }}>
          {chunks.length === 0 ? (
            <div className="p-8 text-center text-[12.5px]" style={{ color: "var(--ink-muted)" }}>
              暂无已上传的知识文档
            </div>
          ) : (
            chunks.map((doc) => {
              const docId = doc.parent_id || doc.chunk_id;
              const isActive = (doc.status || "active") === "active";
              const busy = togglingId === docId;
              return (
                <div
                  key={docId}
                  className={`p-4 flex items-start justify-between gap-4 transition-opacity ${
                    isActive ? "" : "opacity-65"
                  }`}
                >
                  <div className="min-w-0 space-y-1.5 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-semibold text-[13.5px]">{doc.doc_title}</span>
                      <span
                        className="inline-flex items-center gap-1 font-mono text-[10.5px] px-2 py-0.5 rounded-full"
                        style={{
                          background: isActive ? "var(--ok-soft)" : "var(--surface-subtle)",
                          color: isActive ? "var(--ok)" : "var(--ink-muted)",
                        }}
                      >
                        <span
                          className="w-1.5 h-1.5 rounded-full"
                          style={{ background: isActive ? "var(--ok)" : "var(--ink-muted)" }}
                        />
                        {isActive ? "生效中" : "已下架"}
                      </span>
                      {typeof doc.chunk_count === "number" && (
                        <span
                          className="font-mono text-[10.5px] px-1.5 py-0.5 rounded"
                          style={{ background: "var(--surface-subtle)", color: "var(--ink-muted)" }}
                        >
                          {doc.chunk_count} 切片
                        </span>
                      )}
                    </div>
                    {doc.snippet && (
                      <div className="text-[12.5px] leading-relaxed line-clamp-2" style={{ color: "var(--ink-secondary)" }}>
                        {doc.snippet}
                      </div>
                    )}
                  </div>

                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => handleToggleStatus(docId, isActive ? "active" : "disabled")}
                    className="interactive-btn shrink-0 inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-[12px] font-medium disabled:opacity-40"
                    style={{
                      background: isActive ? "var(--bg)" : "var(--ok-soft)",
                      borderColor: isActive ? "var(--border-strong)" : "var(--ok)",
                      color: isActive ? "var(--danger)" : "var(--ok)",
                    }}
                  >
                    {isActive ? <Power size={12} /> : <CheckCircle2 size={12} />}
                    {busy ? "处理中..." : isActive ? "下架" : "生效"}
                  </button>
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
};
