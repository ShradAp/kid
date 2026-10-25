"use client";

import { useEffect, useRef, type ChangeEvent, type KeyboardEvent, type ClipboardEvent } from "react";
import { Plus, Trash2, Square, X, Paperclip } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";

export interface Attachment {
  id: string;
  name: string;
  url: string;
  isImage: boolean;
  isVideo: boolean;
  file: File;
}

export function InputDeck({
  value,
  onChange,
  onSend,
  onStop,
  onClearChat,
  isGenerating,
  attachments,
  onAttachMany,
  onRemoveAttachment,
}: {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  onStop: () => void;
  onClearChat: () => void;
  isGenerating: boolean;
  attachments: Attachment[];
  onAttachMany: (files: File[]) => void;
  onRemoveAttachment: (id: string) => void;
}) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Автоподстройка высоты на ЛЮБОЕ изменение значения — не только на ручной
  // ввод (onInput). Без этого эффекта после отправки многострочного сообщения
  // textarea оставалась растянутой, пока пользователь не введёт новый символ:
  // при программной очистке value="" input-событие не срабатывает.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [value]);

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (
      e.key === "Enter" &&
      !e.shiftKey &&
      !e.nativeEvent.isComposing &&
      (e.nativeEvent as any).keyCode !== 229
    ) {
      e.preventDefault();
      // Во время генерации повторный Enter — ничего не делает:
      // он НЕ должен нажимать кнопку остановки ответа.
      if (isGenerating) return;
      if (value.trim() || attachments.length > 0) onSend();
    }
  }

  function handleInput(e: ChangeEvent<HTMLTextAreaElement>) {
    onChange(e.target.value);
    const el = textareaRef.current;
    if (el) {
      el.style.height = "auto";
      el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
    }
  }

  function handlePaste(e: ClipboardEvent<HTMLTextAreaElement>) {
    const items = e.clipboardData?.items;
    if (!items) return;

    const files: File[] = [];
    for (let i = 0; i < items.length; i++) {
      const item = items[i];
      if (item.kind === "file") {
        const file = item.getAsFile();
        if (file) {
          // Для безымянных вставленных изображений генерируем имя
          const named =
            file.name && file.name !== "image.png"
              ? file
              : new File(
                  [file],
                  `pasted-${Date.now()}.${file.type.split("/")[1] || "bin"}`,
                  { type: file.type },
                );
          files.push(named);
        }
      }
    }

    if (files.length > 0) {
      e.preventDefault();
      onAttachMany(files);
    }
    // Если файлов нет — стандартная вставка текста проходит без изменений
  }

  async function handleFilePickerClick() {
    try {
      const res = await fetch("/api/pick-files", {
        method: "POST",
      });
      if (!res.ok) throw new Error(`File picker returned ${res.status}`);

      const data: { files?: Array<{ path?: string; name?: string }> } =
        await res.json();
      const selectedFiles = (data.files ?? []).filter(
        (item): item is { path: string; name: string } =>
          Boolean(item.path && item.name),
      );
      if (selectedFiles.length > 0) {
        const dummyFiles = selectedFiles.map((item) => {
          const dummy = new File([""], item.name, {
            type: "application/octet-stream",
          });
          Object.defineProperty(dummy, "path", {
            value: item.path,
            enumerable: true,
          });
          return dummy;
        });
        onAttachMany(dummyFiles);
        return;
      }
    } catch {
      // Если нативный диалог недоступен, открываем стандартный браузерный.
    }
    fileInputRef.current?.click();
  }

  function handleFileChange(e: ChangeEvent<HTMLInputElement>) {
    if (e.target.files && e.target.files.length > 0) {
      onAttachMany(Array.from(e.target.files));
    }
    e.target.value = "";
  }

  return (
    <div className="pointer-events-none sticky bottom-0 z-20 px-4 pb-6 pt-2 sm:px-6">
      <div className="pointer-events-auto mx-auto flex w-full max-w-3xl flex-col gap-2">
        {attachments.length > 0 && (
          <div className="flex max-h-28 flex-wrap gap-2 overflow-y-auto px-1 py-1">
            {attachments.map((att) => (
              <div
                key={att.id}
                className="flex items-center gap-2 rounded-xl border border-white/10 bg-card/90 py-1.5 pl-2 pr-1.5 shadow-lg backdrop-blur-md"
              >
                {att.isImage ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={att.url || "/placeholder.svg"}
                    alt={att.name}
                    className="size-8 rounded-md object-cover"
                  />
                ) : (
                  <div className="flex size-8 items-center justify-center rounded-md bg-white/10">
                    <Paperclip className="size-3.5 text-muted-foreground" />
                  </div>
                )}
                <span className="max-w-36 truncate text-xs text-foreground/80">
                  {att.name}
                </span>
                <button
                  type="button"
                  onClick={() => onRemoveAttachment(att.id)}
                  className="rounded-full p-1 text-muted-foreground hover:bg-white/10 hover:text-foreground"
                  aria-label="Remove attachment"
                >
                  <X className="size-3.5" />
                </button>
              </div>
            ))}
          </div>
        )}

        <div className="flex items-end gap-1.5 rounded-3xl border border-white/10 bg-card/95 p-2 shadow-2xl shadow-black/40 backdrop-blur-md">
          <input
            ref={fileInputRef}
            type="file"
            multiple
            className="hidden"
            accept=".mp4,.webm,.mov,.mkv,video/mp4,video/webm,video/quicktime,video/x-matroska,image/*,*/*"
            onChange={handleFileChange}
          />

          <Tooltip>
            <TooltipTrigger
              render={
                <button
                  type="button"
                  className="inline-flex size-8 shrink-0 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-white/10 hover:text-foreground"
                  onClick={handleFilePickerClick}
                  aria-label="Attach file"
                />
              }
            >
              <Plus className="size-4" strokeWidth={2.25} aria-hidden="true" />
            </TooltipTrigger>
            <TooltipContent>Attach file or image</TooltipContent>
          </Tooltip>

          <textarea
            ref={textareaRef}
            value={value}
            onChange={handleInput}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            rows={1}
            placeholder="Message your assistant..."
            className="max-h-40 min-h-9 flex-1 resize-none bg-transparent px-1 py-1.5 text-[15px] text-foreground placeholder:text-muted-foreground focus:outline-none"
          />

          {isGenerating && (
            <Button
              size="icon"
              className="shrink-0 rounded-full bg-white/10 text-foreground hover:bg-white/20"
              onClick={onStop}
              aria-label="Stop generating"
            >
              <Square className="size-3.5 fill-current" />
            </Button>
          )}

          <AlertDialog>
            <Tooltip>
              <TooltipTrigger
                render={
                  <AlertDialogTrigger
                    render={
                      <Button
                        variant="ghost"
                        size="icon"
                        className="shrink-0 rounded-full text-muted-foreground hover:bg-destructive/15 hover:text-destructive"
                        aria-label="Clear chat"
                      />
                    }
                  >
                    <Trash2 />
                  </AlertDialogTrigger>
                }
              />
              <TooltipContent>Clear chat</TooltipContent>
            </Tooltip>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Clear this chat history?</AlertDialogTitle>
                <AlertDialogDescription>
                  This cannot be undone.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Cancel</AlertDialogCancel>
                <AlertDialogAction variant="destructive" onClick={onClearChat}>
                  Clear
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </div>
      </div>
    </div>
  );
}
