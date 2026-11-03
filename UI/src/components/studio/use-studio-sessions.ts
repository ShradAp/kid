"use client";

import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import type { Persona } from "@/lib/assistant-data";
import {
  deleteStudioProfile,
  deleteStudioSession,
  fetchStudioInit,
  saveStudioProfile,
  setStudioActiveProfile,
  upsertStudioSession,
} from "@/lib/studio-data";

export function useStudioSessions({ studioOpen, closeDrawer, openProfiles }: {
  studioOpen: boolean; closeDrawer: () => void; openProfiles: () => void;
}) {
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
  // Профили и чаты студии: загружаются при её открытии (отдельный набор).
  const loadStudio = async () => {
    try {
      const init = await fetchStudioInit();
      const mapped: Persona[] = init.profiles.map((p) => ({
        id: p.id,
        extensionIds: p.extensionIds ?? [],
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
    closeDrawer();
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
    openProfiles();
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
        extensionIds: updated.extensionIds ?? [],
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

  return { studioPersonas, studioActiveId, studioSessions, studioSessionId, setStudioSessionId, handleSelectStudioPersona, handleNewStudioChat, handleDeleteStudioSession, handleCreateStudioPersona, handleSaveStudioPersona, handleDeleteStudioPersona };
}
