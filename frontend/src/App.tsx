import React, { useEffect, useState } from "react";
import { ChatCanvas } from "./features/chat/ChatCanvas";
import { Sidebar } from "./features/chat/Sidebar";
import { useChatSession } from "./features/chat/useChatSession";
import { SettingsModal } from "./features/settings/SettingsModal";
import { InspectorTab, TraceDrawer } from "./features/trace/TraceDrawer";

export default function App() {
  const [dark, setDark] = useState(() => {
    try {
      return window.localStorage.getItem("agent_runtime_theme") !== "light";
    } catch {
      return true;
    }
  });
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("trace");
  const [settingsOpen, setSettingsOpen] = useState(false);

  const session = useChatSession();

  useEffect(() => {
    const theme = dark ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", theme);
    try {
      window.localStorage.setItem("agent_runtime_theme", theme);
    } catch {
      // ignore storage errors
    }
  }, [dark]);

  const handleOpenInspector = (tab: InspectorTab) => {
    setInspectorTab(tab);
    setInspectorOpen(true);
  };

  return (
    <div className="h-screen w-screen flex overflow-hidden">
      <Sidebar
        appMeta={session.appMeta}
        threads={session.threads}
        activeThreadId={session.activeThreadId}
        isDraftThread={session.isDraftThread}
        onSelectThread={session.selectThread}
        onNewThread={session.newThread}
        onDeleteThread={session.removeThread}
        modelConfig={session.modelConfig}
        onOpenSettings={() => setSettingsOpen(true)}
        dark={dark}
        onToggleTheme={() => setDark((d) => !d)}
      />

      <ChatCanvas
        appMeta={session.appMeta}
        threadTitle={session.activeThreadTitle}
        items={session.items}
        loading={session.loading}
        focusNonce={session.composerFocusNonce}
        modelConfig={session.modelConfig}
        inspectorOpen={inspectorOpen}
        onOpenInspector={handleOpenInspector}
        onSend={session.sendMessage}
        onAbort={session.abortRun}
        onUploadFile={session.uploadAttachment}
        onDownloadFile={session.downloadFile}
        onApprovalDecision={session.decideApproval}
        onSetFeedback={session.setFeedback}
        onInspectTrace={(trId) => {
          session.setSelectedTraceId(trId);
          setInspectorTab("trace");
          setInspectorOpen(true);
          session.refreshAll(session.activeThreadId, trId, false);
        }}
      />

      <TraceDrawer
        open={inspectorOpen}
        activeTab={inspectorTab}
        onChangeTab={setInspectorTab}
        onClose={() => setInspectorOpen(false)}
        threadId={session.activeThreadId}
        threadTitle={session.activeThreadTitle}
        isDraftThread={session.isDraftThread}
        state={session.stateSnap}
        onRefresh={() =>
          session.refreshAll(session.activeThreadId, session.selectedTraceId, session.isDraftThread)
        }
        onSelectTrace={(trId) => {
          session.setSelectedTraceId(trId);
          session.refreshAll(session.activeThreadId, trId, false);
        }}
      />

      <SettingsModal
        open={settingsOpen}
        config={session.modelConfig}
        onClose={() => setSettingsOpen(false)}
        onSave={session.saveModelConfig}
      />
    </div>
  );
}
