import React, { useState } from "react";
import { KeyRound, X } from "lucide-react";
import { ModelConfig } from "../../shared/types/runtime";

interface SettingsModalProps {
  open: boolean;
  config: ModelConfig;
  onClose: () => void;
  onSave: (payload: {
    api_key?: string;
    base_url: string;
    model: string;
    mode: "deepseek" | "mock";
  }) => Promise<void>;
}

export const SettingsModal: React.FC<SettingsModalProps> = ({
  open,
  config,
  onClose,
  onSave,
}) => {
  const [mode, setMode] = useState<"deepseek" | "mock">(config.mode);
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState(
    config.base_url || "https://api.deepseek.com/v1"
  );
  const [model, setModel] = useState(config.model || "deepseek-chat");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  if (!open) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError("");
    try {
      await onSave({
        mode,
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
            onClick={onClose}
            aria-label="关闭弹窗"
            className="p-1 rounded-md"
            style={{ color: "var(--ink-muted)" }}
          >
            <X size={16} />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-3.5 text-[13px]">
          <div>
            <label className="block text-[12px] font-medium mb-1.5" style={{ color: "var(--ink-secondary)" }}>
              运行模式
            </label>
            <div className="grid grid-cols-2 gap-2">
              <button
                type="button"
                onClick={() => setMode("deepseek")}
                className="interactive-btn py-2 px-3 rounded-lg border font-medium"
                style={{
                  background: mode === "deepseek" ? "var(--accent-soft)" : "var(--bg)",
                  borderColor: mode === "deepseek" ? "var(--accent)" : "var(--border)",
                  color: mode === "deepseek" ? "var(--accent)" : "var(--ink-secondary)",
                }}
              >
                DeepSeek 真实模型
              </button>
              <button
                type="button"
                onClick={() => setMode("mock")}
                className="interactive-btn py-2 px-3 rounded-lg border font-medium"
                style={{
                  background: mode === "mock" ? "var(--accent-soft)" : "var(--bg)",
                  borderColor: mode === "mock" ? "var(--accent)" : "var(--border)",
                  color: mode === "mock" ? "var(--accent)" : "var(--ink-secondary)",
                }}
              >
                Mock 离线演示
              </button>
            </div>
          </div>

          <div>
            <label className="block text-[12px] font-medium mb-1" style={{ color: "var(--ink-secondary)" }}>
              DeepSeek API Key {config.has_api_key ? "（后端已存在 Key，留空则沿用）" : ""}
            </label>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="sk-..."
              className="w-full px-3 py-2 rounded-lg border font-mono text-[12.5px] outline-none"
              style={{
                background: "var(--bg)",
                borderColor: "var(--border-strong)",
                color: "var(--ink)",
              }}
            />
          </div>

          <div className="grid grid-cols-2 gap-2.5">
            <div>
              <label className="block text-[12px] font-medium mb-1" style={{ color: "var(--ink-secondary)" }}>
                模型名称
              </label>
              <select
                value={model}
                onChange={(e) => setModel(e.target.value)}
                className="w-full px-2.5 py-2 rounded-lg border font-mono text-[12.5px] outline-none"
                style={{
                  background: "var(--bg)",
                  borderColor: "var(--border-strong)",
                  color: "var(--ink)",
                }}
              >
                <option value="deepseek-chat">deepseek-chat</option>
                <option value="deepseek-reasoner">deepseek-reasoner</option>
              </select>
            </div>
            <div>
              <label className="block text-[12px] font-medium mb-1" style={{ color: "var(--ink-secondary)" }}>
                API Base URL
              </label>
              <input
                type="text"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                className="w-full px-2.5 py-2 rounded-lg border font-mono text-[12px] outline-none"
                style={{
                  background: "var(--bg)",
                  borderColor: "var(--border-strong)",
                  color: "var(--ink)",
                }}
              />
            </div>
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
