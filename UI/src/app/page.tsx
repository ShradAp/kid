"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { Header } from "@/components/shell/header";
import { SidebarDrawer } from "@/components/shell/sidebar-drawer";
import { ChatFeed } from "@/components/chat/chat-feed";
import { InputDeck, type Attachment } from "@/components/chat/input-deck";
import { SettingsModal } from "@/components/dialogs/settings-modal";
import { ProfilesModal } from "@/components/dialogs/profiles-modal";
import { NotesPanel, type Note, NOTES_STORAGE_KEY } from "@/components/panels/notes-panel";
import { LogsPanel } from "@/components/panels/logs-panel";
import { FilePreviewPanel } from "@/components/panels/file-preview-panel";
import { OsApprovalModal } from "@/components/panels/os-approval-modal";
import { DocumentStudioView } from "@/components/studio/DocumentStudioView";
import {
  setFileClickMode,
  setFilePreviewListener,
  type FilePreviewRequest,
} from "@/lib/file-actions";
import {
  personas as initialPersonas,
  type Persona,
  type Message,
  type AssistantStatus,
} from "@/lib/assistant-data";
import {
  deleteStudioProfile,
  deleteStudioSession,
  fetchStudioInit,
  saveStudioProfile,
  setStudioActiveProfile,
  upsertStudioSession,
} from "@/lib/studio-data";

const FONT_SIZE_VALUES = ["13px", "16px", "18px", "21px"];

// Локальная копия последнего открытого чата — fallback на случай,
// если бэкенд (например, ручной server_bridge) не хранит active_session.
const ACTIVE_SESSION_STORAGE_KEY = "kid-active-session";

async function extractVideoFrames(file: File, count = 3): Promise<string[]> {
  const sourceUrl = URL.createObjectURL(file);
  const video = document.createElement("video");
  video.preload = "metadata";
  video.muted = true;
  video.playsInline = true;

  try {
    await new Promise<void>((resolve, reject) => {
      video.onloadedmetadata = () => resolve();
      video.onerror = () => reject(new Error("не удалось открыть видео"));
      video.src = sourceUrl;
    });

    const duration = Number.isFinite(video.duration) ? video.duration : 0;
    if (!duration) return [];
    const canvas = document.createElement("canvas");
    const scale = Math.min(1, 1024 / Math.max(video.videoWidth, video.videoHeight));
    canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
    canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
    const context = canvas.getContext("2d");
    if (!context) return [];

    const frames: string[] = [];
    for (let index = 1; index <= count; index++) {
      const time = Math.min(duration - 0.01, (duration * index) / (count + 1));
      await new Promise<void>((resolve, reject) => {
        const onSeeked = () => {
          video.removeEventListener("seeked", onSeeked);
          resolve();
        };
        video.addEventListener("seeked", onSeeked, { once: true });
        video.onerror = () => reject(new Error("не удалось прочитать кадр видео"));
        video.currentTime = Math.max(0, time);
      });
      context.drawImage(video, 0, 0, canvas.width, canvas.height);
      frames.push(canvas.toDataURL("image/jpeg", 0.7));
    }
    return frames;
  } finally {
    URL.revokeObjectURL(sourceUrl);
    video.removeAttribute("src");
    video.load();
  }
}

export default function AssistantPage() {
  // Профили приходят ТОЛЬКО с бэкенда (/api/init). Демо-персоны убраны:
  // пока состояние не загружено, интерфейс не должен показывать фиктивные
  // профили и создавать с их именем чаты.
  const [personas, setPersonas] = useState<Persona[]>(initialPersonas);
  const [activePersonaId, setActivePersonaId] = useState("main");

  const [sessions, setSessions] = useState([
    {
      id: "default",
      personaId: "main",
      title: "Основной диалог",
      timestamp: "Сейчас",
    },
  ]);
  const [activeSessionId, setActiveSessionId] = useState("default");
  const [messagesBySession, setMessagesBySession] = useState<
    Record<string, Message[]>
  >({
    default: [],
  });

  const [drawerOpen, setDrawerOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [profilesOpen, setProfilesOpen] = useState(false);
  const [notesOpen, setNotesOpen] = useState(false);
  const [notes, setNotes] = useState<Note[]>([]);
  const notesLoadedRef = useRef(false);
  const [logsOpen, setLogsOpen] = useState(false);
  const [logs, setLogs] = useState<string[]>([]);
  // Document Studio открывается в этом же окне приложения из бокового меню.
  const [studioOpen, setStudioOpen] = useState(false);
  // Профили и чаты студии: отдельные от основного чата, но управляются тем же
  // интерфейсом (боковая панель + модалка профилей) — при открытии студии
  // основные профили скрываются.
  const [studioPersonas, setStudioPersonas] = useState<Persona[]>([]);
  const [studioActiveId, setStudioActiveId] = useState("");
  const [studioSessions, setStudioSessions] = useState<
    { id: string; personaId: string; title: string; timestamp: string }[]
  >([]);
  const [studioSessionId, setStudioSessionId] = useState("");
  const studioLoadedRef = useRef(false);
  // Версия локальных изменений защищает чат от запоздалого /api/init,

  // который иначе может вернуть старую историю поверх только что полученного ответа.
  const stateMutationRef = useRef(0);
  // Инициализация из бэкенда завершена: только после этого можно запоминать
  // активный чат, иначе стартовый "default" перетёр бы сохранённый чат.
  const stateLoadedRef = useRef(false);
  // Счётчик авто-ретраев /api/init: на старте приложения мост может ещё
  // не слушать порт — повторяем запрос, а не остаёмся с пустым состоянием.
  const fetchRetriesRef = useRef(0);

  // Ввод и вложения хранятся ПО СЕССИИ, чтобы текст в поле не перетекал между чатами.
  const [inputsBySession, setInputsBySession] = useState<Record<string, string>>({});
  const [attachmentsBySession, setAttachmentsBySession] = useState<
    Record<string, Attachment[]>
  >({});
  const [status, setStatus] = useState<AssistantStatus>("ready");
  const [isDragging, setIsDragging] = useState(false);
  // Генерация отслеживается ПО СЕССИИ: можно отправить запрос в одном чате и,
  // пока он думает, отправить другой запрос в соседнем чате.
  const [generatingBySession, setGeneratingBySession] = useState<
    Record<string, boolean>
  >({});
  // Живой прогресс генерации (GET /api/progress): вызовы инструментов
  // отображаются в реальном времени, пока /api/chat ещё не ответил.
  const [liveThinkingBySession, setLiveThinkingBySession] = useState<
    Record<string, string[]>
  >({});
  const [liveReasoningBySession, setLiveReasoningBySession] = useState<
    Record<string, string>
  >({});
  const progressTimersRef = useRef<Record<string, number>>({});
  // Активные запросы /api/chat по сессиям: контроллер отмены + сессия старта.
  // Нужны кнопке «стоп», чтобы разорвать ждущий fetch конкретного чата.
  const chatAbortsRef = useRef<
    Record<string, { controller: AbortController; sessionId: string }>
  >({});
  // Скролл-контейнер ленты сообщений: нужен ChatFeed, чтобы «прилипать» к низу
  // во время вывода «размышлений» и не дёргать ленту, если пользователь прокрутил вверх.
  const feedScrollRef = useRef<HTMLElement>(null);
  const [fontSizeIndex, setFontSizeIndex] = useState(1);
  const [previewRequest, setPreviewRequest] = useState<FilePreviewRequest | null>(
    null,
  );

  // Баланс и токены
  const [balance, setBalance] = useState(0);
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

  // Заметки хранятся на бэкенде, поэтому не зависят от профиля WebView и его кэша.
  useEffect(() => {
    window.localStorage.removeItem("kid-personas");
    fetch("/api/init")
      .then((response) => response.json())
      .then((data) => {
        if (Array.isArray(data.notes)) {
          setNotes((currentNotes) => {
            // Не затираем заметку, созданную ассистентом, если /api/init
            // завершился после ответа /api/chat.
            const nextNotes = currentNotes.length > 0 ? currentNotes : data.notes;
            try {
              window.localStorage.setItem(NOTES_STORAGE_KEY, JSON.stringify(nextNotes));
            } catch {
              // локальный кэш может быть недоступен
            }
            return nextNotes;
          });
        }
        notesLoadedRef.current = true;
      })
      .catch(() => {
        try {
          const raw = window.localStorage.getItem(NOTES_STORAGE_KEY);
          const parsed = raw ? JSON.parse(raw) : [];
          if (Array.isArray(parsed)) setNotes(parsed);
        } catch {
          // игнорируем повреждённое хранилище
        }
        notesLoadedRef.current = true;
      });
  }, []);

  // Локальный кэш ускоряет открытие панели, а серверная копия переживает ребут приложения.
  useEffect(() => {
    if (!notesLoadedRef.current) return;
    try {
      window.localStorage.setItem(NOTES_STORAGE_KEY, JSON.stringify(notes));
    } catch {
      // хранилище может быть недоступно (приватный режим и т.п.)
    }
    fetch("/api/notes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ notes }),
    }).catch(() => undefined);
  }, [notes]);

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

  function handleAddNote(
    title: string,
    content: string,
    author: "user" | "assistant" = "user",
  ) {
    const note: Note = {
      id: `note_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
      title,
      content,
      createdAt: new Date().toLocaleString("ru-RU"),
      author,
    };
    setNotes((prev) => [note, ...prev]);
  }

  function handleDeleteNote(id: string) {
    setNotes((prev) => prev.filter((n) => n.id !== id));
  }

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

  // Принудительное обновление баланса (кнопка Refresh в шапке): лёгкий эндпоинт,
  // который игнорирует TTL-кэш и сразу идёт в SprutDock за актуальным значением.
  const refreshBalance = async () => {
    try {
      const response = await fetch("/api/balance/refresh");
      const data = await response.json();
      if (typeof data.balance === "number") {
        setBalance(data.balance);
      }
    } catch {
      // нет связи с бэкендом — оставляем прежнее значение
    }
  };

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

  function persistSession(id: string, personaId: string, title: string) {
    fetch("/api/session/upsert", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, personaId, title }),
    }).catch(() => {});
  }

  // Запоминает последний открытый чат: на бэкенде (переживает рестарт приложения)
  // и локально (fallback, если бэкенд недоступен или не поддерживает activate).
  function persistActiveSession(id: string) {
    try {
      window.localStorage.setItem(ACTIVE_SESSION_STORAGE_KEY, id);
    } catch {
      // хранилище может быть недоступно (приватный режим и т.п.)
    }
    fetch("/api/session/activate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: id }),
    }).catch(() => {});
  }

  // Смена активного чата (клик в сайдбаре, новый чат, восстановление после
  // рестарта) — сразу фиксируется, чтобы открыться при следующем запуске.
  useEffect(() => {
    if (!stateLoadedRef.current || !activeSessionId) return;
    persistActiveSession(activeSessionId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSessionId]);

  // Загрузка состояния из Python (профили + ВСЕ сессии всех профилей)
  const fetchState = () => {
    const requestedMutationVersion = stateMutationRef.current;
    fetch("/api/init")
      .then((r) => r.json())
      .then((data) => {
        // Не применяем устаревший снимок, если за время запроса пользователь
        // отправил/удалил/очистил чат локально.
        if (requestedMutationVersion !== stateMutationRef.current) return;
        if (data.personas && data.personas.length > 0) {
          setPersonas(data.personas);
          if (data.active_profile) setActivePersonaId(data.active_profile);
        }
        if (typeof data.balance === "number") {
          setBalance(data.balance);
        }
        // Новый формат: sessions + session_messages (все чаты всех профилей)
        if (Array.isArray(data.sessions) && data.sessions.length > 0) {
          const restoredSessions = data.sessions.map((s: any) => ({
            id: String(s.id),
            personaId: String(s.personaId || data.active_profile || "main"),
            title: String(s.title || "Новый чат"),
            timestamp: String(s.timestamp || "Ранее"),
          }));
          setSessions(restoredSessions);
          const msgs: Record<string, Message[]> = {};
          const sm = data.session_messages || {};
          for (const s of restoredSessions) {
            const arr = Array.isArray(sm[s.id]) ? sm[s.id] : [];
            msgs[s.id] = arr
              .filter((m: any) => m.role === "user" || m.role === "assistant")
              .map((m: any, idx: number) => ({
                id: `hist_${s.id}_${idx}`,
                role: m.role,
                content:
                  typeof m.content === "string"
                    ? m.content
                    : JSON.stringify(m.content),
                timestamp: "Ранее",
                attachments: Array.isArray(m.attachments)
                  ? m.attachments
                      .filter((a: any) => a && typeof a.name === "string" && typeof a.url === "string")
                      .map((a: any) => ({ name: a.name, url: a.url }))
                  : undefined,
                reasoning: typeof m.reasoning === "string" ? m.reasoning : undefined,
              }));
          }
          setMessagesBySession(msgs);
          const activePid = String(data.active_profile || "main");
          // Восстановление последнего открытого чата после рестарта приложения:
          // приоритет — сервер (config.json), затем локальный кэш клиента.
          const isFirstLoad = !stateLoadedRef.current;
          let savedSessionId: string | null =
            isFirstLoad && typeof data.active_session === "string" && data.active_session
              ? data.active_session
              : null;
          if (isFirstLoad && !savedSessionId) {
            try {
              savedSessionId = window.localStorage.getItem(ACTIVE_SESSION_STORAGE_KEY);
            } catch {
              savedSessionId = null;
            }
          }
          const savedSession = savedSessionId
            ? restoredSessions.find((s: any) => s.id === savedSessionId)
            : undefined;
          if (savedSession) {
            // Чат мог принадлежать другому профилю — открываем и его профиль.
            if (savedSession.personaId && savedSession.personaId !== activePid) {
              setActivePersonaId(savedSession.personaId);
              fetch("/api/set_active_profile", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ profile_id: savedSession.personaId }),
              }).catch(() => {});
            }
            setActiveSessionId(savedSession.id);
          } else {
            setActiveSessionId((prev) => {
              const cur = restoredSessions.find((s: any) => s.id === prev);
              if (cur && cur.personaId === activePid) return prev;
              const firstOfActive = restoredSessions.find(
                (s: any) => s.personaId === activePid,
              );
              if (firstOfActive) return firstOfActive.id;
              if (restoredSessions.some((s: any) => s.id === "default"))
                return "default";
              return restoredSessions[0].id;
            });
          }
        } else if (
          data.history &&
          Array.isArray(data.history) &&
          data.history.length > 0
        ) {
          // Fallback для старого бэкенда: только default
          const restoredMessages: Message[] = data.history
            .filter((m: any) => m.role === "user" || m.role === "assistant")
            .map((m: any, idx: number) => ({
              id: `hist_${idx}_${Date.now()}`,
              role: m.role,
              content:
                typeof m.content === "string"
                  ? m.content
                  : JSON.stringify(m.content),
              timestamp: "Ранее",
            }));
          setMessagesBySession((prev) => ({
            ...prev,
            default: restoredMessages,
          }));
        }
        // Инициализация завершена — дальше можно запоминать активный чат.
        stateLoadedRef.current = true;
        fetchRetriesRef.current = 0;
      })
      .catch(() => {
        // Мост мог ещё не подняться (холодный старт приложения) — повторяем
        // /api/init, вместо вечного показа пустого состояния.
        if (fetchRetriesRef.current < 5) {
          fetchRetriesRef.current += 1;
          window.setTimeout(fetchState, 2000);
          return;
        }
        // Бэкенд так и не ответил — разрешаем локальное запоминание выбора.
        stateLoadedRef.current = true;
      });
  };

  useEffect(() => {
    fetchState();
  }, []);

  // Профили и чаты студии: загружаются при её открытии (отдельный набор).
  const loadStudio = async () => {
    try {
      const init = await fetchStudioInit();
      const mapped: Persona[] = init.profiles.map((p) => ({
        id: p.id,
        emoji: p.emoji || "📄",
        name: p.name,
        description: p.description,
        systemPrompt: p.systemPrompt,
        temperature: p.temperature,
        baseUrl: p.baseUrl,
        apiKey: "",
        hasApiKey: p.hasApiKey,
        modelName: p.modelName,
      }));
      setStudioPersonas(mapped);
      const activeId = init.active_profile || mapped[0]?.id || "";
      setStudioActiveId(activeId);
      const mappedSessions = init.sessions.map((s) => ({
        id: s.id,
        personaId: s.personaId || activeId,
        title: s.title || "Новый чат",
        timestamp: s.timestamp || "",
      }));
      setStudioSessions(mappedSessions);
      let stored = "";
      try {
        stored = window.localStorage.getItem("kid-studio-last-session") ?? "";
      } catch {
        stored = "";
      }
      const preferred = [stored, init.last_active_session].find(
        (id) => id && mappedSessions.some((s) => s.id === id),
      );
      if (preferred) {
        setStudioSessionId(preferred);
      } else if (mappedSessions[0]) {
        setStudioSessionId(mappedSessions[0].id);
      } else {
        const id = `studio_${Date.now().toString(36)}`;
        setStudioSessions([
          { id, personaId: activeId, title: "Новый чат студии", timestamp: "" },
        ]);
        setStudioSessionId(id);
        void upsertStudioSession({
          id,
          title: "Новый чат студии",
          personaId: activeId,
        }).catch(() => undefined);
      }
    } catch (error) {
      toast.error("Не удалось открыть документ-студию", {
        description: error instanceof Error ? error.message : undefined,
      });
    }
  };

  useEffect(() => {
    if (!studioOpen || studioLoadedRef.current) return;
    studioLoadedRef.current = true;
    void loadStudio();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [studioOpen]);

  const activePersona =
    personas.find((p) => p.id === activePersonaId) ?? personas[0];
  const messages = useMemo(
    () => messagesBySession[activeSessionId] ?? [],
    [messagesBySession, activeSessionId],
  );
  // Ввод, вложения, статус генерации и живой прогресс берутся по активной сессии.
  const input = inputsBySession[activeSessionId] ?? "";
  const attachments = attachmentsBySession[activeSessionId] ?? [];
  const isGenerating = Boolean(generatingBySession[activeSessionId]);
  const liveThinking = liveThinkingBySession[activeSessionId] ?? [];
  const liveReasoning = liveReasoningBySession[activeSessionId] ?? "";
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

  async function handleSelectStudioPersona(id: string) {
    setStudioActiveId(id);
    void setStudioActiveProfile(id).catch(() => undefined);
    const first = studioSessions.find((s) => s.personaId === id);
    if (first) setStudioSessionId(first.id);
    else handleNewStudioChat(id);
  }

  function handleNewStudioChat(personaId: string = studioActiveId) {
    const id = `studio_${Date.now().toString(36)}`;
    setStudioSessions((prev) => [
      { id, personaId, title: "Новый чат студии", timestamp: "Только что" },
      ...prev,
    ]);
    setStudioSessionId(id);
    setDrawerOpen(false);
    try {
      window.localStorage.setItem("kid-studio-last-session", id);
    } catch {
      // localStorage может быть недоступен в ограниченном WebView.
    }
    void upsertStudioSession({ id, title: "Новый чат студии", personaId }).catch(
      () => undefined,
    );
  }

  async function handleDeleteStudioSession(id: string) {
    try {
      await deleteStudioSession(id);
    } catch {
      // Локально удаляем в любом случае.
    }
    const remaining = studioSessions.filter((s) => s.id !== id);
    setStudioSessions(remaining);
    if (studioSessionId === id) {
      const fallback =
        remaining.find((s) => s.personaId === studioActiveId) ?? remaining[0];
      if (fallback) setStudioSessionId(fallback.id);
      else handleNewStudioChat();
    }
  }

  function handleCreateStudioPersona() {
    const id = `studio_${Date.now().toString(36)}`;
    const persona: Persona = {
      id,
      emoji: "📄",
      name: "Новый профиль студии",
      description: "",
      systemPrompt: "",
      temperature: 0.7,
      baseUrl: "",
      apiKey: "",
      modelName: "",
    };
    setStudioPersonas((prev) => [...prev, persona]);
    setStudioActiveId(id);
    void setStudioActiveProfile(id).catch(() => undefined);
    setProfilesOpen(true);
  }

  async function handleSaveStudioPersona(updated: Persona) {
    setStudioPersonas((prev) =>
      prev.some((p) => p.id === updated.id)
        ? prev.map((p) => (p.id === updated.id ? updated : p))
        : [...prev, updated],
    );
    try {
      await saveStudioProfile({
        id: updated.id,
        name: updated.name,
        emoji: updated.emoji,
        description: updated.description,
        modelName: updated.modelName,
        systemPrompt: updated.systemPrompt,
        temperature: updated.temperature,
        baseUrl: updated.baseUrl,
        apiKey: updated.apiKey,
        clearApiKey: updated.clearApiKey,
        requestTimeout: 120,
      });
    } catch (error) {
      toast.error("Не удалось сохранить профиль студии", {
        description: error instanceof Error ? error.message : undefined,
      });
    }
  }

  async function handleDeleteStudioPersona(id: string) {
    if (studioPersonas.length <= 1) {
      toast.error("Нельзя удалить единственный профиль студии");
      return;
    }
    try {
      const res = await deleteStudioProfile(id);
      const remaining = studioPersonas.filter((p) => p.id !== id);
      setStudioPersonas(remaining);
      setStudioActiveId(String(res.active_profile || remaining[0]?.id || ""));
    } catch (error) {
      toast.error("Не удалось удалить профиль студии", {
        description: error instanceof Error ? error.message : undefined,
      });
    }
  }

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
    if (studioOpen) void handleSaveStudioPersona(updated);
    else void handleSavePersona(updated);
  }
  function uiDeletePersona(id: string) {
    if (studioOpen) void handleDeleteStudioPersona(id);
    else void handleDeletePersona(id);
  }

  function handleSelectPersona(id: string) {
    setActivePersonaId(id);
    // Уведомляем бэкенд о смене активного профиля
    fetch("/api/set_active_profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile_id: id }),
    }).catch(() => {});

    const firstSessionForPersona = sessions.find(
      (session) => session.personaId === id,
    );
    if (firstSessionForPersona) {
      setActiveSessionId(firstSessionForPersona.id);
    } else {
      handleNewChat(id);
    }
  }

  function handleSelectSession(id: string) {
    setActiveSessionId(id);
    setDrawerOpen(false);
  }

  function handleNewChat(personaId: string = activePersonaId) {
    // Чат можно создавать только под СУЩЕСТВУЮЩИЙ профиль: пока профили
    // не загружены с бэкенда, создание привязало бы чат к фиктивной персоне.
    if (!personas.some((p) => p.id === personaId)) {
      toast.error("Профили ещё не загружены — подождите пару секунд");
      return;
    }
    // Новый чат под другой персоной: сначала переключаемся на неё, иначе чат
    // закрепился бы за текущим активным профилем, а не за выбранной персоной.
    if (personaId !== activePersonaId) {
      setActivePersonaId(personaId);
      fetch("/api/set_active_profile", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile_id: personaId }),
      }).catch(() => {});
    }
    stateMutationRef.current += 1;
    const id = `s${Date.now()}`;
    setSessions((prev) => [
      { id, personaId, title: "Новый чат", timestamp: "Только что" },
      ...prev,
    ]);
    setMessagesBySession((prev) => ({ ...prev, [id]: [] }));
    setActiveSessionId(id);
    setDrawerOpen(false);
    persistSession(id, personaId, "Новый чат");
  }

  function handleClearChat() {
    stateMutationRef.current += 1;
    const sid = activeSessionId;
    stateMutationRef.current += 1;
    setMessagesBySession((prev) => ({ ...prev, [sid]: [] }));
    fetch("/api/session/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sid }),
    }).catch(() => {});
    toast.success("Чат очищен");
  }

  async function handleDeleteSession(id: string) {
    stateMutationRef.current += 1;
    const remaining = sessions.filter((s) => s.id !== id);
    setSessions(remaining);
    setMessagesBySession((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
    if (activeSessionId === id) {
      const fallback =
        remaining.find((s) => s.personaId === activePersonaId) ?? remaining[0];
      if (fallback) {
        setActiveSessionId(fallback.id);
      } else {
        // Удалили последний чат: сразу создаём новый, иначе активной
        // осталась бы несуществующая сессия, куда нельзя писать.
        handleNewChat();
      }
    }

    // Чат должен быть стёрт НАМЕРТВО на бэкенде (файлы + кэш памяти + мета),
    // иначе он воскреснет при перезагрузке или после ответа ассистента.
    try {
      const res = await fetch("/api/session/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: id }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data?.ok !== true) {
        throw new Error(
          typeof data?.error === "string" && data.error ? data.error : `HTTP ${res.status}`,
        );
      }
      toast.success("Чат удалён");
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "нет связи с сервером Python";
      toast.error(`Чат не удалён на сервере: ${message}`);
      // Сервер не подтвердил удаление — возвращаем честное состояние списка.
      stateMutationRef.current += 1;
      fetchState();
    }
  }

  function handleAttachMany(files: File[]) {
    const newItems: Attachment[] = files.map((file) => ({
      id: `${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
      name: file.name,
      url: file.type.startsWith("image/") || file.type.startsWith("video/")
        ? URL.createObjectURL(file)
        : "",
      isImage: file.type.startsWith("image/"),
      isVideo: file.type.startsWith("video/") || /\.(mp4|webm|mov|mkv)$/i.test(file.name),
      file,
    }));
    const attachSid = activeSessionId;
    setAttachmentsBySession((prev) => ({
      ...prev,
      [attachSid]: [...(prev[attachSid] ?? []), ...newItems],
    }));
  }

  function handleRemoveAttachment(id: string) {
    const attachSid = activeSessionId;
    setAttachmentsBySession((prev) => ({
      ...prev,
      [attachSid]: (prev[attachSid] ?? []).filter((a) => a.id !== id),
    }));
  }

  function stopProgressPolling(sessionId: string) {
    const timer = progressTimersRef.current[sessionId];
    if (timer !== undefined) {
      window.clearInterval(timer);
      delete progressTimersRef.current[sessionId];
    }
    setLiveThinkingBySession((prev) => {
      const next = { ...prev };
      delete next[sessionId];
      return next;
    });
    setLiveReasoningBySession((prev) => {
      const next = { ...prev };
      delete next[sessionId];
      return next;
    });
  }

  function startProgressPolling(sessionId: string) {
    stopProgressPolling(sessionId);
    const poll = async () => {
      // Сессия могла быть остановлена/очищена — тик чужой генерации игнорируем.
      if (progressTimersRef.current[sessionId] === undefined) return;
      try {
        const res = await fetch(
          `/api/progress?session_id=${encodeURIComponent(sessionId)}`,
        );
        if (!res.ok) return;
        const data = (await res.json()) as {
          active?: boolean;
          steps?: unknown;
          reasoning?: unknown;
        };
        if (progressTimersRef.current[sessionId] === undefined) return;
        setLiveThinkingBySession((prev) => ({
          ...prev,
          [sessionId]: Array.isArray(data.steps)
            ? data.steps.map((step) => String(step))
            : [],
        }));
        setLiveReasoningBySession((prev) => ({
          ...prev,
          [sessionId]:
            typeof data.reasoning === "string" ? data.reasoning : "",
        }));
      } catch {
        // Мост занят обработкой /api/chat — молча ждём следующий тик.
      }
    };
    progressTimersRef.current[sessionId] = window.setInterval(poll, 1000);
    void poll();
  }

  async function handleSend() {
    // Повторная отправка во время генерации невозможна: кнопка «стоп»
    // не должна превращаться в «отправить», а Enter уже блокирован в InputDeck.
    if (isGenerating) return;
    const sessionId = activeSessionId;
    // Фиксируем ввод/вложения на момент отправки: даже если пользователь
    // переключится на другой чат во время загрузки файлов, текст уйдёт в нужную сессию.
    const sessionInput = input;
    const sessionAttachments = attachments;
    if (!sessionInput.trim() && sessionAttachments.length === 0) return;
    // Профили обязаны быть загружены: без них нет ни модели, ни ключа,
    // ни системного промпта — запрос ушёл бы «в никуда».
    if (personas.length === 0) {
      toast.error("Профили ещё не загружены — подождите пару секунд");
      return;
    }

    let accumulatedFilesPrefix = "";
    const payloadAttachments: any[] = [];
    const sentAttachments: Array<{ name: string; url: string }> = [];
    // Файлы загружаются отдельным multipart-запросом, поэтому не попадают в JSON
    // чата и не дублируются в истории последующих сообщений.
    const MAX_IMAGES = activePersona?.attachMaxImages ?? 4;
    let imageCount = 0;
    const skippedFiles: string[] = [];

    async function uploadAttachment(file: File) {
      const form = new FormData();
      form.append("file", file, file.name);
      const response = await fetch("/api/upload", {
        method: "POST",
        body: form,
      });
      if (!response.ok) throw new Error(`ошибка загрузки (${response.status})`);
      return response.json() as Promise<{ file_id: string; name: string; mime: string }>;
    }

    for (const item of sessionAttachments) {
      const file = item.file;
      const nativePath = (file as any)?.path;
      const mime = file.type || "application/octet-stream";

      if (nativePath) {
        payloadAttachments.push({ path: nativePath, name: file.name, mime });
        sentAttachments.push({
          name: file.name,
          url: `/api/download-local?path=${encodeURIComponent(String(nativePath))}&name=${encodeURIComponent(file.name)}`,
        });
      } else if (mime.startsWith("image/")) {
        if (imageCount >= MAX_IMAGES) {
          skippedFiles.push(`${file.name}: превышен лимит медиафайлов`);
          continue;
        }
        imageCount++;
        try {
          const uploaded = await uploadAttachment(file);
          payloadAttachments.push({ file_id: uploaded.file_id, name: uploaded.name, mime: uploaded.mime });
          sentAttachments.push({ name: uploaded.name, url: `/api/download/${encodeURIComponent(uploaded.file_id)}` });
        } catch (error) {
          skippedFiles.push(`${file.name}: не удалось загрузить файл`);
        }
      } else if (mime.startsWith("video/") || item.isVideo) {
        try {
          const uploaded = await uploadAttachment(file);
          payloadAttachments.push({ file_id: uploaded.file_id, name: uploaded.name, mime: uploaded.mime });
          sentAttachments.push({ name: uploaded.name, url: `/api/download/${encodeURIComponent(uploaded.file_id)}` });
        } catch (error) {
          skippedFiles.push(`${file.name}: не удалось загрузить видео`);
        }
      } else {
        try {
          const uploaded = await uploadAttachment(file);
          payloadAttachments.push({ file_id: uploaded.file_id, name: uploaded.name, mime: uploaded.mime });
          sentAttachments.push({ name: uploaded.name, url: `/api/download/${encodeURIComponent(uploaded.file_id)}` });
        } catch (error) {
          skippedFiles.push(`${file.name}: не удалось загрузить файл`);
        }
      }
    }

    if (skippedFiles.length > 0) {
      accumulatedFilesPrefix += `Пропущенные файлы: ${skippedFiles.join(", ")}\n\n`;
      // Пользователь должен видеть, что файлы не дошли до модели.
      toast.error("Часть файлов не прикреплена", {
        description: skippedFiles.join("\n"),
      });
    }
    const filesNamesText = sessionAttachments.map((a) => a.name).join(", ");
    const displayText =
      sessionInput.trim() ||
      (sessionAttachments.length > 0 ? `Отправлены файлы: ${filesNamesText}` : "");
    const finalPromptForBackend =
      accumulatedFilesPrefix +
      (sessionInput.trim() || "Проанализируй прикрепленные файлы.");

    const userMessage: Message = {
      id: `m${Date.now()}`,
      role: "user",
      content: displayText,
      timestamp: new Date().toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      }),
      attachments: sentAttachments,
    };

    // Устаревший /api/init не должен вернуть старую историю поверх нового запроса.
    stateMutationRef.current += 1;
    setMessagesBySession((prev) => ({
      ...prev,
      [sessionId]: [...(prev[sessionId] ?? []), userMessage],
    }));

    setInputsBySession((prev) => ({ ...prev, [sessionId]: "" }));
    setAttachmentsBySession((prev) => ({ ...prev, [sessionId]: [] }));
    setGeneratingBySession((prev) => ({ ...prev, [sessionId]: true }));
    setStatus("thinking");
    startProgressPolling(sessionId);
    // Контроллер отмены: кнопка «стоп» разорвёт этот fetch и сообщит бэкенду.
    const chatController = new AbortController();
    chatAbortsRef.current[sessionId] = { controller: chatController, sessionId };

    // Финальный ответ (или ошибка) ВСЕГДА попадает в ленту активного чата:
    // ни при каком сбое сообщение не должно молча исчезнуть.
    let assistantPushed = false;
    // Баланс из тела ответа: если его там не окажется (сбой сборки ответа,
    // транспортная ошибка), в finally подтянем его отдельным запросом.
    let balanceReceived = false;
    const pushAssistant = (
      text: string,
      thinking?: string[],
      marketplaceProduct?: Message["marketplaceProduct"],
      reasoning?: string,
    ) => {
      assistantPushed = true;
      const message: Message = {
        id: `m${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
        role: "assistant",
        content: text,
        timestamp: new Date().toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
        }),
        thinking: thinking && thinking.length ? thinking : undefined,
        reasoning: reasoning && reasoning.trim() ? reasoning : undefined,
        marketplaceProduct,
      };
      setMessagesBySession((prev) => ({
        ...prev,
        [sessionId]: [...(prev[sessionId] ?? []), message],
      }));
    };

    try {
      const firstText = (displayText || finalPromptForBackend || "")
        .replace(/\s+/g, " ")
        .trim()
        .slice(0, 48);
      const payload: any = {
        session_id: sessionId,
        prompt: finalPromptForBackend,
        persona_id: activePersona.id,
        max_steps: activePersona?.maxSteps ?? 60,
        attachments: payloadAttachments,
        title: firstText || "Новый чат",
      };
      // сразу показываем осмысленный заголовок в сайдбаре
      if (firstText) {
        const sid = sessionId;
        const pid = activePersona.id;
        setSessions((prev) =>
          prev.map((s) =>
            s.id === sid && (s.title === "Новый чат" || s.title === "New chat")
              ? { ...s, title: firstText }
              : s,
          ),
        );
        persistSession(sid, pid, firstText);
      }

      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: chatController.signal,
      });

      // Читаем тело текстом: даже частично битый JSON не должен терять ответ.
      const rawBody = await res.text();
      // Пользователь нажал «стоп», пока мы ждали тело — ответ не показываем.
      if (chatController.signal.aborted) return;
      let data: any = {};
      try {
        data = rawBody ? JSON.parse(rawBody) : {};
      } catch {
        data = {};
      }
      if (!res.ok) {
        throw new Error(
          typeof data?.error === "string" && data.error
            ? data.error
            : `HTTP ${res.status}`,
        );
      }

      if (typeof data.balance === "number") {
        setBalance(data.balance);
        balanceReceived = true;
      }

      const replyText =
        typeof data.reply === "string" && data.reply.trim()
          ? data.reply
          : "Ответ получен пустой.";

      // Ответ относится к сессии, в которой был отправлен запрос: пушим именно
      // в неё и сразу рендерим — статус загрузки снимается в finally.
      pushAssistant(
        replyText,
        Array.isArray(data.thinking) ? data.thinking : undefined,
        data.marketplaceProduct || undefined,
        typeof data.reasoning === "string" ? data.reasoning : undefined,
      );

      // Заметки от ассистента (tool save_note): поддерживаем пачку notes[]
      // и старый формат note, но не добавляем совместимый дубликат дважды.
      const incomingNotes: { title?: unknown; content?: unknown }[] = Array.isArray((data as any).notes)
        ? (data as any).notes
        : data.note && typeof (data as any).note.content === "string"
          ? [(data as any).note]
          : [];
      if (incomingNotes.length > 0) {
        for (const n of incomingNotes) {
          if (!n || typeof n.content !== "string" || !n.content.trim()) continue;
          handleAddNote(
            typeof n.title === "string" && n.title.trim()
              ? n.title.trim().slice(0, 120)
              : "Заметка от ассистента",
            n.content.trim().slice(0, 4000),
            "assistant",
          );
        }
        toast.success(
          incomingNotes.length > 1
            ? `Сохранено заметок: ${incomingNotes.length}`
            : "Заметка сохранена",
        );
      }

      if (logsOpen) fetchLogs();
    } catch (err) {
      // «Стоп» — штатная отмена: не пушим ошибку в ленту и не показываем тост.
      if (chatController.signal.aborted) return;
      const message = err instanceof Error ? err.message : "Ошибка связи с сервером Python";
      // Гарантия: в ленте всегда появляется ответ — даже при ошибке транспорта,
      // иначе финальный ответ ассистента просто пропадал бы из интерфейса.
      if (!assistantPushed) {
        pushAssistant(`⚠️ **Не удалось получить ответ:** ${message}`);
      }
      toast.error(message);
    } finally {
      stopProgressPolling(sessionId);
      setGeneratingBySession((prev) => ({ ...prev, [sessionId]: false }));
      setStatus("ready");
      // Баланс обязан обновиться после КАЖДОГО ответа модели (в т.ч. в режиме
      // ассистента): значение из ответа уже применено, а если его в теле не было
      // (сбой сборки, ошибка транспорта) — подтягиваем актуальный с бэкенда.
      if (chatAbortsRef.current[sessionId]?.controller === chatController)
        delete chatAbortsRef.current[sessionId];
      if (!balanceReceived) void refreshBalance();
    }
  }

  function handleStop() {
    const sid = activeSessionId;
    const active = chatAbortsRef.current[sid];
    if (!active) return;
    // 1) Бэкенд: выставляем событие отмены — цикл генерации прервётся между
    //    шагами, а долгий вызов LLM перестанет ожидаться (поток бросается).
    void fetch("/api/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: active.sessionId }),
      keepalive: true,
    }).catch(() => undefined);
    // 2) Разрываем ждущий fetch /api/chat: поздний ответ не будет показан.
    active.controller.abort();
    delete chatAbortsRef.current[sid];
    stopProgressPolling(sid);
    setGeneratingBySession((prev) => ({ ...prev, [sid]: false }));
    setStatus("ready");
    // Пометка в ленте той сессии, где шла генерация: ответ остановлен кнопкой.
    const stoppedMessage: Message = {
      id: `m${Date.now()}_stop`,
      role: "assistant",
      content: "⏹ Генерация остановлена.",
      timestamp: new Date().toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      }),
    };
    setMessagesBySession((prev) => ({
      ...prev,
      [active.sessionId]: [...(prev[active.sessionId] ?? []), stoppedMessage],
    }));
  }

  async function handleDeletePersona(id: string) {
    if (personas.length <= 1) {
      toast.error("Нельзя удалить единственный профиль");
      return;
    }
    try {
      const res = await fetch("/api/delete_persona", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile_id: id }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      const remaining = personas.filter((item) => item.id !== id);
      setPersonas(remaining);
      if (activePersonaId === id) handleSelectPersona(String(data.active_profile || remaining[0].id));
      toast.success("Профиль удалён");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Не удалось удалить профиль");
    }
  }

  function handleCreatePersona() {
    const id = `profile_${Date.now()}`;
    const newPersona: Persona = {
      id,
      emoji: "✨",
      name: "Новый профиль",
      description: "Пользовательский профиль ассистента.",
      systemPrompt: "",
      temperature: 0.7,
      baseUrl: "",
      apiKey: "",
      modelName: "",
    };
    setPersonas((prev) => [...prev, newPersona]);
    setActivePersonaId(id);
    setProfilesOpen(true);
    setDrawerOpen(false);
  }

  async function handleSavePersona(updated: Persona) {
    // Сохранять можно только профиль, реально пришедший с бэкенда: иначе
    // устаревшая/демо-копия персоны затёрла бы настоящие настройки.
    if (!personas.some((p) => p.id === updated.id)) {
      toast.error("Профиль не загружен — сохранение отменено");
      return;
    }
    // Обновляем стейт немедленно
    setPersonas((prev) => prev.map((p) => (p.id === updated.id ? updated : p)));
    try {
      window.localStorage.removeItem("kid-personas");
    } catch {
      // localStorage may be unavailable in restricted WebViews
    }

    // Сохраняем на бэкенд, чтобы изменения пережили перезапуск
    try {
      const res = await fetch("/api/save_personas", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ persona: updated }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        toast.error(`Ошибка сохранения профиля: ${err.error ?? res.status}`);
      }
    } catch {
      toast.error("Нет связи с сервером — профиль сохранён только в памяти");
    }
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
                messages={messages}
                isThinking={isGenerating}
                liveSteps={liveThinking}
                liveReasoning={liveReasoning}
                fontSizeIndex={fontSizeIndex}
                scrollContainerRef={feedScrollRef}
              />
            </main>

            <InputDeck
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
          open={profilesOpen}
          onOpenChange={setProfilesOpen}
          persona={uiActivePersona}
          onSave={uiSavePersona}
        />
      )}
    </div>
  );
}
