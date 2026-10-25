"use client";

import { useEffect, useState } from "react";
import { Download, FileCode, FileText, FileType, ExternalLink, X } from "lucide-react";
import { toast } from "sonner";
import type { FilePreviewRequest } from "@/lib/file-actions";

function formatIcon(format: string) {
  if (format === "py") return FileCode;
  if (format === "docx") return FileType;
  return FileText;
}

const IMAGE_EXT = /\.(png|jpe?g|gif|webp|bmp|svg|ico|avif)$/i;
const VIDEO_EXT = /\.(mp4|webm|mov|mkv|avi|m4v)$/i;
const TEXT_EXT =
  /\.(txt|md|markdown|py|js|mjs|cjs|ts|tsx|jsx|json|csv|html?|css|log|ya?ml|xml|sh|bat|cmd|sql|ini|cfg|conf|toml|tex|r|java|c|cpp|h|hpp|go|rs|rb|php|swift|kt|kts|vue|svelte)$/i;

type AttachmentKind = "image" | "video" | "text" | "other";

function detectKind(filename: string): AttachmentKind {
  if (IMAGE_EXT.test(filename)) return "image";
  if (VIDEO_EXT.test(filename)) return "video";
  if (TEXT_EXT.test(filename)) return "text";
  return "other";
}

// Из URL вложения достаёт ссылку для нативного «Сохранить как» (/api/save-file):
// загруженные файлы -> file_id, файлы с диска -> path. null, если URL не подходит.
function extractSaveReference(
  url: string,
): { file_id?: string; path?: string } | null {
  try {
    const parsed = new URL(url);
    if (parsed.pathname.startsWith("/api/download/")) {
      const fileId = decodeURIComponent(
        parsed.pathname.slice("/api/download/".length),
      );
      return fileId ? { file_id: fileId } : null;
    }
    if (parsed.pathname === "/api/download-local") {
      const path = parsed.searchParams.get("path");
      return path ? { path } : null;
    }
  } catch {
    // не URL — используем blob-скачивание
  }
  return null;
}

export function FilePreviewPanel({
  request,
  onClose,
}: {
  request: FilePreviewRequest | null;
  onClose: () => void;
}) {
  const [content, setContent] = useState("");
  const [name, setName] = useState("");
  const [format, setFormat] = useState("txt");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [kind, setKind] = useState<AttachmentKind>("other");
  const [mediaUrl, setMediaUrl] = useState("");

  const isAttachment = Boolean(request?.url);
  const Icon = formatIcon(format);

  useEffect(() => {
    if (!request) {
      setContent("");
      setName("");
      setFormat("txt");
      setError("");
      setKind("other");
      setMediaUrl("");
      return;
    }
    let cancelled = false;
    let objectUrl = "";
    setLoading(true);
    setError("");
    setName(request.filename);
    setContent("");
    setMediaUrl("");
    setFormat("txt");
    setKind("other");

    // Вложение: контент тянем по прямому URL (fetch не слушает Content-Disposition).
    if (request.url) {
      const kind = detectKind(request.filename);
      const ext = request.filename.split(".").pop()?.toLowerCase() || "txt";
      setKind(kind);
      setFormat(ext);
      if (kind === "image" || kind === "video") {
        fetch(request.url)
          .then((response) => {
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            return response.blob();
          })
          .then((blob) => {
            if (cancelled) return;
            objectUrl = URL.createObjectURL(blob);
            setMediaUrl(objectUrl);
          })
          .catch((err) => {
            if (!cancelled) setError(err instanceof Error ? err.message : "Ошибка загрузки");
          })
          .finally(() => {
            if (!cancelled) setLoading(false);
          });
      } else if (kind === "text") {
        fetch(request.url)
          .then((response) => {
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            return response.blob();
          })
          .then((blob) => blob.text())
          .then((text) => {
            if (!cancelled) setContent(text);
          })
          .catch((err) => {
            if (!cancelled) setError(err instanceof Error ? err.message : "Ошибка загрузки");
          })
          .finally(() => {
            if (!cancelled) setLoading(false);
          });
      } else {
        // Непредпросматриваемый тип (docx/pdf/zip и т.п.) — только кнопка скачивания.
        setLoading(false);
      }
      return () => {
        cancelled = true;
        if (objectUrl) URL.revokeObjectURL(objectUrl);
      };
    }

    // Сгенерированный файл: прежний путь через /api/generated/<id>/preview.
    if (!request.fileId) {
      setLoading(false);
      setError("Не указан источник файла");
      return;
    }
    fetch(
      `/api/generated/${encodeURIComponent(request.fileId)}/preview`,
    )
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then((data: { name?: string; format?: string; content?: string }) => {
        if (cancelled) return;
        if (typeof data.content === "string") setContent(data.content);
        if (typeof data.format === "string") setFormat(data.format);
        if (typeof data.name === "string" && data.name) setName(data.name);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Ошибка загрузки");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [request]);

  useEffect(() => {
    if (!request) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [request, onClose]);

  async function handleOpenInSystem() {
    if (!request?.fileId) return;
    try {
      const response = await fetch(
        `/api/generated/${encodeURIComponent(request.fileId)}/open`,
        { method: "POST" },
      );
      if (!response.ok) {
        const data = (await response.json().catch(() => ({}))) as {
          error?: string;
        };
        throw new Error(data.error || `HTTP ${response.status}`);
      }
    } catch (err) {
      toast.error("Не удалось открыть файл", {
        description: err instanceof Error ? err.message : undefined,
      });
    }
  }

  async function handleDownloadAttachment() {
    if (!request?.url) return;
    // 1) Нативное «Сохранить как» через бэкенд — надёжно в WebView2, где
    //    скриптовое скачивание blob-ссылки может молча игнорироваться.
    const reference = extractSaveReference(request.url);
    if (reference) {
      try {
        const res = await fetch("/api/save-file", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...reference, name: request.filename }),
        });
        if (res.ok) {
          const data = (await res.json().catch(() => ({}))) as {
            path?: string;
            auto?: boolean;
          };
          if (data.auto && data.path) {
            toast.success("Файл сохранён", { description: data.path });
          }
          return; // сохранено либо диалог отменён пользователем
        }
      } catch {
        // старый мост без /api/save-file — падаем на blob-скачивание ниже
      }
    }
    // 2) Фолбэк: blob-скачивание через объектную ссылку (обычный браузер).
    try {
      const response = await fetch(request.url);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = name || request.filename || "file";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 2000);
    } catch (err) {
      toast.error("Не удалось скачать файл", {
        description: err instanceof Error ? err.message : undefined,
      });
    }
  }

  if (!request) return null;

  return (
    <aside className="file-preview-panel flex w-[440px] shrink-0 flex-col border-l border-white/10 bg-[#121218]">
      <div className="flex shrink-0 items-center gap-2 border-b border-white/10 px-4 py-3">
        <Icon className="size-4 shrink-0 text-primary" />
        <span className="min-w-0 flex-1 truncate text-sm font-semibold text-foreground">
          {name}
        </span>
        <span className="shrink-0 rounded bg-white/10 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
          {format}
        </span>
        {isAttachment && (
          <button
            type="button"
            onClick={() => void handleDownloadAttachment()}
            className="rounded-md p-1.5 text-muted-foreground transition hover:bg-white/10 hover:text-foreground"
            aria-label="Скачать файл"
            title="Скачать"
          >
            <Download className="size-4" />
          </button>
        )}
        {!isAttachment && (
          <button
            type="button"
            onClick={() => void handleOpenInSystem()}
            className="rounded-md p-1.5 text-muted-foreground transition hover:bg-white/10 hover:text-foreground"
            aria-label="Открыть в системе"
            title="Открыть в системе (Word/Блокнот)"
          >
            <ExternalLink className="size-4" />
          </button>
        )}
        <button
          type="button"
          onClick={onClose}
          className="rounded-md p-1.5 text-muted-foreground transition hover:bg-white/10 hover:text-foreground"
          aria-label="Закрыть панель"
          title="Закрыть"
        >
          <X className="size-4" />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden px-5 py-5">
        {isAttachment && kind === "image" ? (
          <div className="flex h-full items-center justify-center">
            {loading ? (
              <p className="text-sm text-neutral-400">Загрузка…</p>
            ) : error ? (
              <p className="text-sm text-red-500">
                Не удалось загрузить предпросмотр: {error}
              </p>
            ) : mediaUrl ? (
              <img
                src={mediaUrl}
                alt={name}
                className="max-h-full max-w-full rounded-md object-contain"
              />
            ) : (
              <p className="text-sm text-neutral-400">Файл пуст</p>
            )}
          </div>
        ) : isAttachment && kind === "video" ? (
          <div className="flex h-full items-center justify-center">
            {loading ? (
              <p className="text-sm text-neutral-400">Загрузка…</p>
            ) : error ? (
              <p className="text-sm text-red-500">
                Не удалось загрузить предпросмотр: {error}
              </p>
            ) : mediaUrl ? (
              <video src={mediaUrl} controls className="max-h-full max-w-full rounded-md" />
            ) : (
              <p className="text-sm text-neutral-400">Файл пуст</p>
            )}
          </div>
        ) : (
          <div className="w-full rounded-sm border border-black/10 bg-white shadow-[0_6px_24px_rgba(0,0,0,0.4)]">
            <div className="px-8 py-8">
              {loading ? (
                <p className="text-sm text-neutral-400">Загрузка…</p>
              ) : error ? (
                <p className="text-sm text-red-500">
                  Не удалось загрузить предпросмотр: {error}
                </p>
              ) : content ? (
                <pre className="whitespace-pre-wrap break-words font-sans text-sm leading-relaxed text-neutral-800">
                  {content}
                </pre>
              ) : isAttachment && kind === "other" ? (
                <div className="flex flex-col items-start gap-2">
                  <p className="text-sm text-neutral-500">
                    Предпросмотр недоступен для этого типа файла.
                  </p>
                  <button
                    type="button"
                    onClick={() => void handleDownloadAttachment()}
                    className="inline-flex items-center gap-1.5 rounded-md bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-neutral-700"
                  >
                    <Download className="size-3.5" />
                    Скачать файл
                  </button>
                </div>
              ) : (
                <p className="text-sm text-neutral-400">Файл пуст</p>
              )}
            </div>
          </div>
        )}
      </div>
    </aside>
  );
}
