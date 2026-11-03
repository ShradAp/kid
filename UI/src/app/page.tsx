"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { useStudioSessions } from "@/components/studio/use-studio-sessions";
import { useChatSessions } from "@/components/chat/use-chat-sessions";
import { useNotes } from "@/components/panels/use-notes";
import { useChatGeneration } from "@/components/chat/use-chat-generation";
import { Header } from "@/components/shell/header";
import { SidebarDrawer } from "@/components/shell/sidebar-drawer";
import { ChatFeed } from "@/components/chat/chat-feed";
import { InputDeck } from "@/components/chat/input-deck";
import { SettingsModal } from "@/components/dialogs/settings-modal";
import { ProfilesModal } from "@/components/dialogs/profiles-modal";
import { CodePanel } from "@/components/panels/code-panel";
import { NotesPanel, type Note } from "@/components/panels/notes-panel";
import { LogsPanel } from "@/components/panels/logs-panel";
import { FilePreviewPanel } from "@/components/panels/file-preview-panel";
import { OsApprovalModal } from "@/components/panels/os-approval-modal";
import { DocumentStudioView } from "@/components/studio/DocumentStudioView";
import {
  setFileClickMode,
  setFilePreviewListener,
  type FilePreviewRequest,
} from "@/lib/file-actions";
import { type Persona } from "@/lib/assistant-data";

const FONT_SIZE_VALUES = ["13px", "16px", "18px", "21px"];

export default function AssistantPage() {

  const [drawerOpen, setDrawerOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [profilesOpen, setProfilesOpen] = useState(false);
  const [codeOpen, setCodeOpen] = useState(false);
  const [notesOpen, setNotesOpen] = useState(false);
  const { notes, setNotes, handleAddNote, handleDeleteNote } = useNotes();
  const [logsOpen, setLogsOpen] = useState(false);
  const [logs, setLogs] = useState<string[]>([]);

  // Загрузка логов бэкенда
  const fetchLogs = () => {
    fetch("/api/logs")
      .then((r) => r.json())
      .then((data) => {
        if (Array.isArray(data.logs)) {
          setLogs(data.logs);
        }
      })
      .catch(() => {});
  };
  // Document Studio открывается в этом же окне приложения из бокового меню.
  const [studioOpen, setStudioOpen] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  // Скролл-контейнер ленты сообщений: нужен ChatFeed, чтобы «прилипать» к низу
  // во время вывода «размышлений» и не дёргать ленту, если пользователь прокрутил вверх.
  const feedScrollRef = useRef<HTMLElement>(null);
  const [fontSizeIndex, setFontSizeIndex] = useState(1);
  const [previewRequest, setPreviewRequest] = useState<FilePreviewRequest | null>(
    null,
  );
  const {
    activePersona,
    personas,
    activePersonaId,
    sessions,
    setSessions,
    activeSessionId,
    messagesBySession,
    setMessagesBySession,
    stateMutationRef,
    balance,
    setBalance,
    fetchState,
    refreshBalance,
    persistSession,
    handleSelectPersona,
    handleSelectSession,
    handleNewChat,
    handleClearChat,
    handleDeleteSession,
    handleDeletePersona,
    handleCreatePersona,
    handleSavePersona,
  } = useChatSessions({ closeDrawer: () => setDrawerOpen(false), openProfiles: () => setProfilesOpen(true) });
  const {
    input,
    attachments,
    status,
    isGenerating,
    liveThinking,
    liveReasoning,
    liveReplyBySession,
    setInputsBySession,
    handleAttachMany,
    handleRemoveAttachment,
    handleSend,
    handleStop,
  } = useChatGeneration({
    activePersona,
    activeSessionId,
    personas,
    setMessagesBySession,
    setSessions,
    stateMutationRef,
    persistSession,
    setBalance,
    refreshBalance,
    handleAddNote,
    setNotes,
    logsOpen,
    fetchLogs,
  });
  const {
    studioPersonas,
    studioActiveId,
    studioSessions,
    studioSessionId,
    setStudioSessionId,
    handleSelectStudioPersona,
    handleNewStudioChat,
    handleDeleteStudioSession,
    handleCreateStudioPersona,
    handleSaveStudioPersona,
    handleDeleteStudioPersona,
  } = useStudioSessions({ studioOpen, closeDrawer: () => setDrawerOpen(false), openProfiles: () => setProfilesOpen(true) });
  const tokensTotal = 1000000;

  // Считаем токены строго по активному открытому чату
  const tokensUsed = useMemo(() => {
    const activeMsgs = messagesBySession[activeSessionId] ?? [];
    const chars = activeMsgs.reduce(
      (acc, m) => acc + (typeof m.content === "string" ? m.content.length : 0),
      0,
    );
    return Math.floor(chars / 4);
  }, [messagesBySession, activeSessionId]);

  // Режим клика по скрепке (скачивание/предпросмотр) — глобальная настройка с бэкенда.
  useEffect(() => {
    fetch("/api/file-click-mode")
      .then((r) =>
        r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)),
      )
      .then((data: { mode?: string }) => {
        if (data && (data.mode === "download" || data.mode === "preview")) {
          setFileClickMode(data.mode);
        }
      })
      .catch(() => {});
  }, []);

  // Панель предпросмотра открывается по запросу из рендера сообщений.
  useEffect(() => {
    setFilePreviewListener((request) => setPreviewRequest(request));
    return () => setFilePreviewListener(null);
  }, []);

  function handleAttachNote(note: Note) {
    const rawTitle = (note.title || "заметка").trim() || "заметка";
    const safeBase = rawTitle
      .replace(/[\\/:*?"<>|]+/g, "_")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, 80) || "заметка";
    const fileName = safeBase.toLowerCase().endsWith(".md")
      ? safeBase
      : `${safeBase}.md`;
    const body = `# ${rawTitle}\n\n${note.content}`;
    const file = new File([body], fileName, {
      type: "text/markdown",
    });
    handleAttachMany([file]);
    setNotesOpen(false);
    toast.success(`Заметка «${rawTitle}» прикреплена файлом`);
  }

  function handleToggleLogs() {
    setLogsOpen((v) => {
      const next = !v;
      if (next) {
        fetchLogs();
        setNotesOpen(false);
        setDrawerOpen(false);
      }
      return next;
    });
  }

  // Открытие/закрытие панели заметок с взаимоисключением остальных боковых панелей.
  function handleToggleNotes() {
    setNotesOpen((v) => {
      const next = !v;
      if (next) {
        setDrawerOpen(false);
        setLogsOpen(false);
      }
      return next;
    });
  }

  // Режим отображения студии: тот же экран приложения, отдельного окна нет.
  function handleToggleStudio() {
    setDrawerOpen(false);
    setNotesOpen(false);
    setLogsOpen(false);
    setStudioOpen((open) => !open);
  }
  const messages = useMemo(
    () => messagesBySession[activeSessionId] ?? [],
    [messagesBySession, activeSessionId],
  );
  const personaSessions = useMemo(
    () => sessions.filter((session) => session.personaId === activePersonaId),
    [sessions, activePersonaId],
  );

  // --- Document Studio: свой набор профилей/чатов, но тот же интерфейс -------
  // При открытой студии боковая панель и модалка профилей работают с профилями
  // студии, а профили основного приложения скрыты.
  const uiPersonas = studioOpen ? studioPersonas : personas;
  const uiActivePersonaId = studioOpen ? studioActiveId : activePersonaId;
  const uiAllSessions = studioOpen ? studioSessions : sessions;
  const uiPersonaSessions = useMemo(
    () => uiAllSessions.filter((session) => session.personaId === uiActivePersonaId),
    [uiAllSessions, uiActivePersonaId],
  );
  const uiActivePersona =
    uiPersonas.find((p) => p.id === uiActivePersonaId) ?? uiPersonas[0];
  const uiActiveSessionId = studioOpen ? studioSessionId : activeSessionId;

  function uiSelectPersona(id: string) {
    if (studioOpen) void handleSelectStudioPersona(id);
    else handleSelectPersona(id);
  }
  function uiNewChat(personaId?: string) {
    if (studioOpen) handleNewStudioChat();
    else handleNewChat(personaId);
  }
  function uiSelectSession(id: string) {
    if (studioOpen) {
      setStudioSessionId(id);
      setDrawerOpen(false);
    } else {
      handleSelectSession(id);
    }
  }
  function uiDeleteSession(id: string) {
    if (studioOpen) void handleDeleteStudioSession(id);
    else void handleDeleteSession(id);
  }
  function uiCreatePersona() {
    if (studioOpen) handleCreateStudioPersona();
    else handleCreatePersona();
  }
  function uiSavePersona(updated: Persona) {
    return studioOpen ? handleSaveStudioPersona(updated) : handleSavePersona(updated);
  }
  function uiDeletePersona(id: string) {
    if (studioOpen) void handleDeleteStudioPersona(id);
    else void handleDeletePersona(id);
  }

  return (
    <div
      className="relative flex h-dvh flex-col overflow-hidden bg-background select-text"
      onDragOver={(e) => {
        if (studioOpen) return;
        e.preventDefault();
        e.stopPropagation();
        setIsDragging(true);
      }}
      onDragLeave={(e) => {
        if (studioOpen) return;
        e.preventDefault();
        e.stopPropagation();
        if (e.currentTarget.contains(e.relatedTarget as Node)) return;
        setIsDragging(false);
      }}
      onDrop={(e) => {
        if (studioOpen) return;
        e.preventDefault();
        e.stopPropagation();
        setIsDragging(false);
        const droppedFiles = e.dataTransfer.files;
        if (droppedFiles && droppedFiles.length > 0) {
          handleAttachMany(Array.from(droppedFiles));
          toast.success(`Прикреплено файлов: ${droppedFiles.length}`);
        }
      }}
    >
      {isDragging && (
        <div className="pointer-events-none absolute inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm border-2 border-dashed border-primary">
          <div className="rounded-xl bg-card p-6 text-center shadow-2xl">
            <p className="text-base font-semibold text-primary">
              Отпустите файлы для прикрепления
            </p>
            <p className="text-xs text-muted-foreground mt-1">
              Документы, изображения, код
            </p>
          </div>
        </div>
      )}

      <Header
        codexProfileId={!studioOpen && activePersona?.engine === "codex" ? activePersona.id : undefined}
        onOpenCode={() => setCodeOpen(true)}
        onOpenDrawer={() => {
          setDrawerOpen(true);
          setNotesOpen(false);
          setLogsOpen(false);
        }}
        balance={balance}
        tokensUsed={tokensUsed}
        tokensTotal={tokensTotal}
        status={status}
        onRefreshBalance={refreshBalance}
        notesCount={notes.length}
        notesOpen={notesOpen}
        onToggleNotes={handleToggleNotes}
        studioOpen={studioOpen}
        onToggleStudio={handleToggleStudio}
        logsOpen={logsOpen}
        onToggleLogs={handleToggleLogs}
      />

      <LogsPanel
        open={logsOpen}
        logs={logs}
        onClose={() => setLogsOpen(false)}
        onRefresh={fetchLogs}
      />

      {activePersona?.engine === "codex" && !studioOpen && <CodePanel key={activePersona.id} open={codeOpen} onClose={() => setCodeOpen(false)} profileId={activePersona.id} sessionId={activeSessionId} onRecover={() => fetchState()} />}

      <NotesPanel
        open={notesOpen}
        notes={notes}
        onAddNote={(title, content) => handleAddNote(title, content, "user")}
        onDeleteNote={handleDeleteNote}
        onAttachNote={handleAttachNote}
        onClose={() => setNotesOpen(false)}
      />

      <div className="flex min-h-0 flex-1">
        {studioOpen ? (
          <DocumentStudioView
            fontSizeIndex={fontSizeIndex}
            sessionId={studioSessionId}
          />
        ) : (
          <div className="flex min-h-0 min-w-0 flex-1 flex-col">
            <main
              ref={feedScrollRef}
              className="flex flex-1 flex-col overflow-y-auto"
              style={
                {
                  "--chat-font-size": FONT_SIZE_VALUES[fontSizeIndex],
                } as React.CSSProperties
              }
            >
              <ChatFeed
                codex={activePersona?.engine === "codex"}
                messages={messages}
                isThinking={isGenerating}
                liveSteps={liveThinking}
                liveReasoning={liveReasoning}
                liveReply={liveReplyBySession[activeSessionId] ?? ""}
                fontSizeIndex={fontSizeIndex}
                scrollContainerRef={feedScrollRef}
              />
            </main>

            <InputDeck
              sessionId={activeSessionId}
              onVoiceText={(text) => setInputsBySession((prev) => {
                const current = prev[activeSessionId] ?? "";
                const separator = current && !/\s$/.test(current) ? " " : "";
                return { ...prev, [activeSessionId]: current + separator + text };
              })}
              value={input}
              onChange={(v) =>
                setInputsBySession((prev) => ({ ...prev, [activeSessionId]: v }))
              }
              onSend={handleSend}
              onStop={handleStop}
              onClearChat={handleClearChat}
              isGenerating={isGenerating}
              attachments={attachments}
              onAttachMany={handleAttachMany}
              onRemoveAttachment={handleRemoveAttachment}
            />
          </div>
        )}

        <FilePreviewPanel
          request={previewRequest}
          onClose={() => setPreviewRequest(null)}
        />
        <OsApprovalModal />
      </div>

      <SidebarDrawer
        useProfileAccents={!studioOpen}
        open={drawerOpen}
        onOpenChange={setDrawerOpen}
        personas={uiPersonas}
        activePersonaId={uiActivePersonaId}
        onSelectPersona={uiSelectPersona}
        onEditProfiles={() => {
          setDrawerOpen(false);
          setProfilesOpen(true);
        }}
        onCreatePersona={uiCreatePersona}
        onDeletePersona={uiDeletePersona}
        sessions={uiPersonaSessions}
        allSessions={uiAllSessions}
        activeSessionId={uiActiveSessionId}
        onSelectSession={uiSelectSession}
        onNewChat={uiNewChat}
        onDeleteSession={uiDeleteSession}
        onOpenSettings={() => {
          setDrawerOpen(false);
          setSettingsOpen(true);
        }}
      />

      <SettingsModal
        open={settingsOpen}
        onOpenChange={setSettingsOpen}
        fontSizeIndex={fontSizeIndex}
        onFontSizeChange={setFontSizeIndex}
      />

      {/* Модалка монтируется только с реальным профилем: при пустом списке
          (состояние ещё грузится) persona была бы undefined и роняла SSR. */}
      {uiActivePersona && (
        <ProfilesModal
          onDelete={studioOpen ? undefined : handleDeletePersona}
          canDelete={personas.length > 1}
          allowCodex={!studioOpen}
          open={profilesOpen}
          onOpenChange={setProfilesOpen}
          persona={uiActivePersona}
          onSave={uiSavePersona}
        />
      )}
    </div>
  );
}
