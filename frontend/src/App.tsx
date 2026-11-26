import React, { useEffect, useState } from "react";
import { ChatCanvas } from "./features/chat/ChatCanvas";
import { Sidebar } from "./features/chat/Sidebar";
import { useChatSession } from "./features/chat/useChatSession";
import { OpsPlatform } from "./features/ops/OpsPlatform";
import { SettingsModal } from "./features/settings/SettingsModal";
import { TraceDrawer } from "./features/trace/TraceDrawer";

export default function App() {
  const [route, setRoute] = useState<"chat" | "ops">(() =>
    window.location.pathname.startsWith("/ops") ? "ops" : "chat"
  );
  const [dark, setDark] = useState(false);
  const [traceOpen, setTraceOpen] = useState(true);
  const [settingsOpen, setSettingsOpen] = useState(false);

  const session = useChatSession(route);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  }, [dark]);

  useEffect(() => {
    const onPop = () => {
      setRoute(window.location.pathname.startsWith("/ops") ? "ops" : "chat");
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const navigate = (next: "chat" | "ops") => {
    const targetPath = next === "ops" ? "/ops" : "/";
    if (window.location.pathname !== targetPath) {
      window.history.pushState({}, "", targetPath);
    }
    setRoute(next);
  };

  if (route === "ops") {
    return (
      <OpsPlatform
        dark={dark}
        onToggleTheme={() => setDark((d) => !d)}
        onBackToChat={(jumpThreadId) => {
          navigate("chat");
          if (jumpThreadId) session.selectThread(jumpThreadId);
        }}
      />
    );
  }

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
        onOpenOpsPlatform={() => navigate("ops")}
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
        traceOpen={traceOpen}
        onToggleTrace={() => setTraceOpen((v) => !v)}
        onSend={session.sendMessage}
        onAbort={session.abortRun}
        onApprovalDecision={session.decideApproval}
        onInspectTrace={(trId) => {
          session.setSelectedTraceId(trId);
          setTraceOpen(true);
          session.refreshAll(session.activeThreadId, trId, false);
        }}
      />

      <TraceDrawer
        open={traceOpen}
        state={session.stateSnap}
        onRefresh={() =>
          session.refreshAll(session.activeThreadId, session.selectedTraceId, session.isDraftThread)
        }
        onSelectTrace={(trId) => {
          session.setSelectedTraceId(trId);
          session.refreshAll(session.activeThreadId, trId, false);
        }}
        onOpenOpsPlatform={() => navigate("ops")}
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
