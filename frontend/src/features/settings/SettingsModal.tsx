import React, { useEffect, useRef, useState } from "react";
import { Check, ChevronDown, KeyRound, X } from "lucide-react";
import { ModelConfig } from "../../shared/types/runtime";

const MODEL_OPTIONS = [
  { value: "deepseek-chat", label: "deepseek-chat", desc: "通用对话与工具调用" },
  { value: "deepseek-reasoner", label: "deepseek-reasoner", desc: "深度推理（R1）" },
] as const;

interface SettingsModalProps {
  open: boolean;
  config: ModelConfig;
  onClose: () => void;
  onSave: (payload: {
    api_key?: string;
    base_url: string;
    model: string;
    mode: "deepseek";
  }) => Promise<void>;
}

export const SettingsModal: React.FC<SettingsModalProps> = ({
  open,
  config,
  onClose,
  onSave,
}) => {
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState(
    config.base_url || "https://api.deepseek.com/v1"
  );
  const [model, setModel] = useState(config.model || "deepseek-chat");
  const [menuOpen, setMenuOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open) {
      setBaseUrl(config.base_url || "https://api.deepseek.com/v1");
      setModel(config.model || "deepseek-chat");
      setApiKey("");
      setError("");
      setMenuOpen(false);
    }
  }, [open, config.base_url, config.model]);

  useEffect(() => {
    if (!menuOpen) return;
    const onDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, [menuOpen]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (menuOpen) setMenuOpen(false);
        else onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, menuOpen, onClose]);

  if (!open) return null;

  const selectedOption =
    MODEL_OPTIONS.find((opt) => opt.value === model) || {
      value: model,
      label: model,
      desc: "自定义模型",
    };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!config.has_api_key && !apiKey.trim()) {
      setError("请填写 DeepSeek API Key 后再保存");
      return;
    }
    setSaving(true);
    setError("");
    try {
      await onSave({
        mode: "deepseek",
        base_url: baseUrl.trim(),
        model: model.trim(),
        ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
      });
      setApiKey("");
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: "rgba(15, 14, 13, 0.55)" }}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="w-full max-w-md rounded-xl border p-5 shadow-xl"
        style={{ background: "var(--surface)", borderColor: "var(--border-strong)" }}
      >
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <KeyRound size={16} style={{ color: "var(--accent)" }} />
            <h2 className="text-[15px] font-semibold">DeepSeek 模型与密钥热配置</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭弹窗"
            className="p-1 rounded-md"
            style={{ color: "var(--ink-muted)" }}
          >
            <X size={16} />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-3.5 text-[13px]">
          <div
            className="flex items-center justify-between px-3 py-2 rounded-lg border text-[12px]"
            style={{
              background: config.has_api_key ? "var(--ok-soft)" : "var(--warn-soft)",
              borderColor: "var(--border)",
              color: config.has_api_key ? "var(--ok)" : "var(--warn)",
            }}
          >
            <span className="font-medium">
              {config.has_api_key ? "DeepSeek API 已接入" : "尚未配置 DeepSeek API Key"}
            </span>
            <span className="font-mono text-[11px]">{config.model}</span>
          </div>

          <div>
            <label className="block text-[12px] font-medium mb-1.5" style={{ color: "var(--ink-secondary)" }}>
              DeepSeek API Key {config.has_api_key ? "（后端已存在 Key，留空则沿用）" : ""}
            </label>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="sk-..."
              className="w-full h-[38px] px-3 rounded-lg border font-mono text-[12.5px] outline-none"
              style={{
                background: "var(--bg)",
                borderColor: "var(--border-strong)",
                color: "var(--ink)",
              }}
            />
          </div>

          <div ref={menuRef} className="relative">
            <label className="block text-[12px] font-medium mb-1.5" style={{ color: "var(--ink-secondary)" }}>
              模型名称
            </label>
            <button
              type="button"
              onClick={() => setMenuOpen((o) => !o)}
              aria-haspopup="listbox"
              aria-expanded={menuOpen}
              className="w-full h-[38px] px-3 rounded-lg border flex items-center justify-between gap-2 text-left transition-colors"
              style={{
                background: "var(--bg)",
                borderColor: menuOpen ? "var(--accent)" : "var(--border-strong)",
                color: "var(--ink)",
              }}
            >
              <span className="flex items-center gap-2 min-w-0">
                <span className="font-mono text-[12.5px] truncate">{selectedOption.label}</span>
                <span className="text-[11px] truncate" style={{ color: "var(--ink-muted)" }}>
                  {selectedOption.desc}
                </span>
              </span>
              <ChevronDown
                size={14}
                className="shrink-0 transition-transform"
                style={{
                  color: "var(--ink-muted)",
                  transform: menuOpen ? "rotate(180deg)" : "rotate(0deg)",
                }}
              />
            </button>

            {menuOpen && (
              <div
                role="listbox"
                className="absolute left-0 right-0 mt-1.5 z-20 rounded-lg border p-1 shadow-xl space-y-0.5"
                style={{
                  background: "var(--surface)",
                  borderColor: "var(--border-strong)",
                }}
              >
                {MODEL_OPTIONS.map((opt) => {
                  const active = opt.value === model;
                  return (
                    <button
                      key={opt.value}
                      type="button"
                      role="option"
                      aria-selected={active}
                      onClick={() => {
                        setModel(opt.value);
                        setMenuOpen(false);
                      }}
                      className="w-full px-2.5 py-2 rounded-md flex items-center justify-between gap-2 text-left transition-colors"
                      style={{
                        background: active ? "var(--accent-soft)" : "transparent",
                        color: active ? "var(--accent)" : "var(--ink)",
                      }}
                    >
                      <div className="min-w-0">
                        <div className="font-mono text-[12.5px] font-medium truncate">
                          {opt.label}
                        </div>
                        <div className="text-[11px] truncate" style={{ color: "var(--ink-muted)" }}>
                          {opt.desc}
                        </div>
                      </div>
                      {active && <Check size={14} className="shrink-0" style={{ color: "var(--accent)" }} />}
                    </button>
                  );
                })}
              </div>
            )}
          </div>

          <div>
            <label className="block text-[12px] font-medium mb-1.5" style={{ color: "var(--ink-secondary)" }}>
              API Base URL
            </label>
            <input
              type="text"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              className="w-full h-[38px] px-3 rounded-lg border font-mono text-[12.5px] outline-none"
              style={{
                background: "var(--bg)",
                borderColor: "var(--border-strong)",
                color: "var(--ink)",
              }}
            />
          </div>

          {error && (
            <div
              className="px-3 py-2 rounded-lg text-[12px]"
              style={{ background: "var(--danger-soft)", color: "var(--danger)" }}
            >
              {error}
            </div>
          )}

          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="interactive-btn px-3.5 py-2 rounded-lg border"
              style={{ borderColor: "var(--border-strong)", color: "var(--ink-secondary)" }}
            >
              取消
            </button>
            <button
              type="submit"
              disabled={saving}
              className="interactive-btn px-4 py-2 rounded-lg font-medium text-white"
              style={{ background: "var(--accent)" }}
            >
              {saving ? "保存中..." : "保存并热生效"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
