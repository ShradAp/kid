"use client";

import { useState } from "react";
import { NotebookPen, Paperclip, Plus, Trash2, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";

export const NOTES_STORAGE_KEY = "rab_assistant_notes";

export interface Note {
  id: string;
  title: string;
  content: string;
  createdAt: string;
  author: "user" | "assistant";
}

export function NotesPanel({
  open,
  notes,
  onAddNote,
  onDeleteNote,
  onAttachNote,
  onClose,
}: {
  open: boolean;
  notes: Note[];
  onAddNote: (title: string, content: string) => void;
  onDeleteNote: (id: string) => void;
  onAttachNote: (note: Note) => void;
  onClose: () => void;
}) {
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");

  function handleSubmit() {
    if (!content.trim()) return;
    onAddNote(title.trim() || "Без названия", content.trim());
    setTitle("");
    setContent("");
  }

  return (
    <Sheet open={open} onOpenChange={(nextOpen) => !nextOpen && onClose()}>
      <SheetContent
        side="right"
        showCloseButton={false}
        className="flex w-80 flex-col gap-0 border-white/10 bg-sidebar p-0"
      >
        <SheetHeader className="flex h-16 shrink-0 flex-row items-center justify-between gap-2 border-b border-white/10 px-4 py-3">
          <SheetTitle className="flex items-center gap-2 text-sm font-semibold text-foreground">
            <NotebookPen className="size-4 text-primary" />
            <span>Заметки</span>
            {notes.length > 0 && (
              <span className="rounded-full bg-white/10 px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
                {notes.length}
              </span>
            )}
          </SheetTitle>
          <button
            type="button"
            onClick={onClose}
            className="rounded-full p-1 text-muted-foreground transition hover:bg-white/10 hover:text-foreground"
            aria-label="Close notes"
          >
            <X className="size-4" />
          </button>
        </SheetHeader>

        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          {notes.length === 0 ? (
            <div className="px-4 py-6 text-center text-xs text-muted-foreground">
              Пока нет заметок. Добавьте первую ниже.
            </div>
          ) : (
            <ul className="flex flex-col gap-1">
              {notes.map((note) => (
                <li
                  key={note.id}
                  className="group flex flex-col gap-1 rounded-xl px-3 py-2.5 transition-colors hover:bg-white/[0.06]"
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex min-w-0 items-center gap-1.5">
                      <span
                        className={cn(
                          "shrink-0 rounded-full px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide",
                          note.author === "assistant"
                            ? "bg-primary/20 text-primary"
                            : "bg-white/10 text-muted-foreground",
                        )}
                      >
                        {note.author === "assistant" ? "AI" : "Вы"}
                      </span>
                      <span className="truncate text-xs font-semibold text-foreground">
                        {note.title}
                      </span>
                    </div>
                    <div className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
                      <button
                        type="button"
                        onClick={() => onAttachNote(note)}
                        className="rounded-md p-1.5 text-muted-foreground transition hover:bg-primary/15 hover:text-primary"
                        aria-label="Attach note as file"
                        title="Прикрепить файлом к чату"
                      >
                        <Paperclip className="size-3.5" />
                      </button>
                      <button
                        type="button"
                        onClick={() => onDeleteNote(note.id)}
                        className="rounded-md p-1.5 text-muted-foreground transition hover:bg-destructive/15 hover:text-destructive"
                        aria-label="Delete note"
                        title="Удалить"
                      >
                        <Trash2 className="size-3.5" />
                      </button>
                    </div>
                  </div>
                  <p className="whitespace-pre-wrap text-xs leading-relaxed text-foreground/80">
                    {note.content}
                  </p>
                  <span className="text-[10px] text-muted-foreground">
                    {note.createdAt}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="flex flex-col gap-2 border-t border-white/10 p-3">
          <Input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Заголовок (необязательно)"
            className="h-7 text-xs"
          />
          <Textarea
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder="Текст заметки..."
            className="min-h-16 resize-none text-xs"
          />
          <Button
            size="sm"
            className="self-end gap-1"
            onClick={handleSubmit}
            disabled={!content.trim()}
          >
            <Plus className="size-3.5" />
            Добавить
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  );
}
