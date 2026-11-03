"use client";

import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { type Note, NOTES_STORAGE_KEY } from "@/components/panels/notes-panel";

export function useNotes() {
  const [notes, setNotes] = useState<Note[]>([]);
  const notesLoadedRef = useRef(false);
  const notesSaveQueueRef = useRef<Promise<void>>(Promise.resolve());
  const notesLastSnapshotRef = useRef<string | null>(null);
  // Заметки хранятся на бэкенде, поэтому не зависят от профиля WebView и его кэша.
  useEffect(() => {
    window.localStorage.removeItem("kid-personas");
    fetch("/api/notes")
      .then((response) => {
        if (!response.ok) throw new Error("Не удалось загрузить заметки");
        return response.json();
      })
      .then((data) => {
        if (Array.isArray(data.notes)) {
          setNotes((currentNotes) => {
            // Не затираем заметку, созданную ассистентом, если /api/notes
            // завершился после ответа /api/chat.
            const ids = new Set(currentNotes.map((note) => note.id));
            const nextNotes = [...currentNotes, ...data.notes.filter((note: Note) => !ids.has(note.id))];
            // Hydrating an unchanged snapshot is a read, not a user edit.
            if (currentNotes.length === 0) notesLastSnapshotRef.current = JSON.stringify(nextNotes);
            return nextNotes;
          });
        }
        notesLoadedRef.current = true;
      })
      .catch(() => {
        try {
          const raw = window.localStorage.getItem(NOTES_STORAGE_KEY);
          const parsed = raw ? JSON.parse(raw) : [];
          if (Array.isArray(parsed)) setNotes((currentNotes) => {
            const ids = new Set(currentNotes.map((note) => note.id));
            const restored = [...currentNotes, ...parsed.filter((note: Note) => !ids.has(note.id))];
            if (currentNotes.length === 0) notesLastSnapshotRef.current = JSON.stringify(restored);
            return restored;
          });
        } catch {
          // игнорируем повреждённое хранилище
        }
        notesLoadedRef.current = true;
      });
  }, []);
  // Локальный кэш ускоряет открытие панели, а серверная копия переживает ребут приложения.
  useEffect(() => {
    if (!notesLoadedRef.current) return;
    const snapshot = JSON.stringify(notes);
    let localCopySaved = false;
    try {
      window.localStorage.setItem(NOTES_STORAGE_KEY, snapshot);
      localCopySaved = true;
    } catch {
      // хранилище может быть недоступно (приватный режим и т.п.)
    }
    if (snapshot === notesLastSnapshotRef.current) return;
    notesLastSnapshotRef.current = snapshot;
    const body = JSON.stringify({ notes });
    // Full snapshots must arrive in edit order: a slow older request must not
    // overwrite a newer saved note list.
    notesSaveQueueRef.current = notesSaveQueueRef.current.catch(() => undefined).then(async () => {
      const response = await fetch("/api/notes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
      });
      if (!response.ok) throw new Error("Не удалось сохранить заметки");
    }).catch(() => {
      toast.error(localCopySaved
        ? "Не удалось сохранить заметки на сервере. Локальная копия сохранена."
        : "Не удалось сохранить заметки. Копия остаётся только в открытом окне.");
    });
  }, [notes]);
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

  return { notes, setNotes, handleAddNote, handleDeleteNote };
}
