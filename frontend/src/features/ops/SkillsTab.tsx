import React, { useRef, useState } from "react";
import { CheckCircle2, Eye, FileText, Power, Upload, X } from "lucide-react";
import { fetchSkillContent, updateSkillStatus, uploadSkill } from "../../shared/api/client";
import { SkillSummary } from "../../shared/types/runtime";
import { SkillDetailDrawer, SkillDrawerData } from "./SkillDetailDrawer";

interface SkillsTabProps {
  skills: SkillSummary[];
  onRefresh: () => Promise<void>;
}

function filenameToSlug(filename: string): string {
  return filename
    .replace(/\.[^.]+$/, "")
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

export const SkillsTab: React.FC<SkillsTabProps> = ({ skills, onRefresh }) => {
  const [slug, setSlug] = useState("");
  const [desc, setDesc] = useState("");
  const [content, setContent] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [statusMsg, setStatusMsg] = useState("");
  const [isError, setIsError] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [togglingSlug, setTogglingSlug] = useState<string | null>(null);
  const [drawerSkill, setDrawerSkill] = useState<SkillDrawerData | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleFilePick = (picked: File | null) => {
    setFile(picked);
    if (picked && !slug.trim()) {
      const guessed = filenameToSlug(picked.name);
      if (guessed && guessed !== "skill") {
        setSlug(guessed);
      }
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!slug.trim() || !desc.trim() || (!file && !content.trim())) return;

    setUploading(true);
    setStatusMsg("");
    setIsError(false);
    try {
      const res = await uploadSkill({
        slug: slug.trim(),
        description: desc.trim(),
        content: file ? undefined : content,
        file,
      });
      setStatusMsg(`已发布 ${res.skill.slug} v${res.skill.version_num}`);
      setSlug("");
      setDesc("");
      setContent("");
      setFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      await onRefresh();
    } catch (err) {
      setIsError(true);
      setStatusMsg(err instanceof Error ? err.message : "发布失败");
    } finally {
      setUploading(false);
    }
  };

  const handleOpenDrawer = async (sk: SkillSummary) => {
    const curStatus: "active" | "disabled" = sk.status === "disabled" ? "disabled" : "active";
    setDrawerSkill({
      slug: sk.slug,
      version: sk.version_num,
      status: curStatus,
      description: sk.description,
      content: "",
      loading: true,
    });
    try {
      const data = await fetchSkillContent(sk.slug, "", true);
      setDrawerSkill({
        slug: data.skill.slug,
        version: data.skill.version_num,
        status: data.skill.status === "disabled" ? "disabled" : "active",
        description: data.skill.description,
        content: data.content,
        loading: false,
      });
    } catch (err) {
      setDrawerSkill({
        slug: sk.slug,
        version: sk.version_num,
        status: curStatus,
        description: sk.description,
        content: err instanceof Error ? err.message : "读取失败",
        loading: false,
      });
    }
  };

  const handleToggleStatus = async (targetSlug: string, nextStatus: "active" | "disabled") => {
    setTogglingSlug(targetSlug);
    try {
      await updateSkillStatus(targetSlug, nextStatus);
      setDrawerSkill((cur) => (cur && cur.slug === targetSlug ? { ...cur, status: nextStatus } : cur));
      await onRefresh();
    } finally {
      setTogglingSlug(null);
    }
  };

  const activeCount = skills.filter((s) => (s.status || "active") === "active").length;

  return (
    <>
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-5">
        <form
          onSubmit={handleSubmit}
          className="lg:col-span-4 rounded-xl border p-4 space-y-3.5 h-fit"
          style={{ background: "var(--surface)", borderColor: "var(--border)" }}
        >
          <div className="flex items-center gap-2 font-semibold text-[13.5px]">
            <Upload size={15} style={{ color: "var(--accent)" }} />
            上传 / 发布 Skill
          </div>

          <input
            ref={fileInputRef}
            type="file"
            accept=".md,.txt,.json,.yaml,.yml,text/markdown,text/plain"
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
              className="interactive-btn w-full py-5 px-4 rounded-xl border border-dashed text-center space-y-1 cursor-pointer"
              style={{
                background: dragging ? "var(--accent-soft)" : "var(--bg)",
                borderColor: dragging ? "var(--accent)" : "var(--border-strong)",
              }}
            >
              <Upload size={17} className="mx-auto" style={{ color: "var(--accent)" }} />
              <div className="text-[12.5px] font-medium">点击或拖拽 SKILL.md 文件上传</div>
              <div className="text-[11px]" style={{ color: "var(--ink-muted)" }}>
                支持 .md / .txt 声明式文件（禁止脚本代码）
              </div>
            </div>
          )}

          <input
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
            placeholder="slug（小写字母/数字/-），如：weekly-report"
            className="w-full px-3 py-2 rounded-lg border font-mono text-[12px] outline-none"
            style={{ background: "var(--bg)", borderColor: "var(--border-strong)", color: "var(--ink)" }}
          />
          <input
            value={desc}
            onChange={(e) => setDesc(e.target.value)}
            placeholder="一句话简介（供模型检索发现）"
            className="w-full px-3 py-2 rounded-lg border text-[13px] outline-none"
            style={{ background: "var(--bg)", borderColor: "var(--border-strong)", color: "var(--ink)" }}
          />

          {!file && (
            <textarea
              rows={5}
              value={content}
              onChange={(e) => setContent(e.target.value)}
              placeholder="或直接输入 SKILL.md 声明式 Markdown SOP 正文..."
              className="w-full px-3 py-2 rounded-lg border font-mono text-[12px] outline-none resize-none"
              style={{ background: "var(--bg)", borderColor: "var(--border-strong)", color: "var(--ink)" }}
            />
          )}

          <button
            type="submit"
            disabled={uploading || !slug.trim() || !desc.trim() || (!file && !content.trim())}
            className="interactive-btn w-full py-2.5 rounded-lg font-medium text-white text-[13px] disabled:opacity-40"
            style={{ background: "var(--accent)" }}
          >
            {uploading ? "发布版本中..." : "发布并生效"}
          </button>
          {statusMsg && (
            <div className="font-mono text-[11.5px]" style={{ color: isError ? "var(--danger)" : "var(--ok)" }}>
              {statusMsg}
            </div>
          )}
        </form>

        <div className="lg:col-span-8">
          <div
            className="rounded-xl border overflow-hidden"
            style={{ background: "var(--surface)", borderColor: "var(--border)" }}
          >
            <div
              className="px-4 py-3 border-b flex items-center justify-between gap-2"
              style={{ borderColor: "var(--border)" }}
            >
              <span className="font-semibold text-[13.5px]">已发布技能 ({skills.length})</span>
              <span className="font-mono text-[11.5px]" style={{ color: "var(--ink-muted)" }}>
                生效中 {activeCount} · 已下架 {skills.length - activeCount}
              </span>
            </div>
            <div className="divide-y" style={{ borderColor: "var(--border)" }}>
              {!skills.length ? (
                <div className="p-8 text-center text-[12.5px]" style={{ color: "var(--ink-muted)" }}>
                  暂无已发布的 Skill
                </div>
              ) : (
                skills.map((sk) => {
                  const isActive = (sk.status || "active") === "active";
                  const busy = togglingSlug === sk.slug;
                  return (
                    <div
                      key={sk.slug}
                      className={`p-4 flex items-center justify-between gap-4 transition-opacity ${
                        isActive ? "" : "opacity-65"
                      }`}
                    >
                      <button
                        type="button"
                        onClick={() => handleOpenDrawer(sk)}
                        className="min-w-0 flex-1 text-left space-y-1 group"
                      >
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="font-mono font-semibold text-[13.5px] group-hover:underline">
                            {sk.slug}
                          </span>
                          <span
                            className="font-mono text-[10.5px] px-1.5 py-0.5 rounded"
                            style={{ background: "var(--surface-subtle)", color: "var(--ink-secondary)" }}
                          >
                            v{sk.version_num}
                          </span>
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
                        </div>
                        <div className="text-[12.5px]" style={{ color: "var(--ink-secondary)" }}>
                          {sk.description}
                        </div>
                      </button>

                      <div className="flex items-center gap-2 shrink-0">
                        <button
                          type="button"
                          onClick={() => handleOpenDrawer(sk)}
                          className="interactive-btn inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border text-[12px]"
                          style={{ background: "var(--bg)", borderColor: "var(--border-strong)" }}
                        >
                          <Eye size={12} /> 查看
                        </button>
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => handleToggleStatus(sk.slug, isActive ? "disabled" : "active")}
                          className="interactive-btn inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border text-[12px] font-medium disabled:opacity-40"
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
                    </div>
                  );
                })
              )}
            </div>
          </div>
        </div>
      </div>

      <SkillDetailDrawer
        skill={drawerSkill}
        toggling={Boolean(drawerSkill && togglingSlug === drawerSkill.slug)}
        onToggleStatus={handleToggleStatus}
        onClose={() => setDrawerSkill(null)}
      />
    </>
  );
};
