export type FileClickMode = "download" | "preview";

export interface FilePreviewRequest {
  /** file_id сгенерированного файла (предпросмотр/открытие через /api/generated). */
  fileId?: string;
  filename: string;
  /** Прямой URL содержимого — используется для предпросмотра вложений. */
  url?: string;
}

let currentMode: FileClickMode = "download";
let previewListener: ((request: FilePreviewRequest) => void) | null = null;

export function getFileClickMode(): FileClickMode {
  return currentMode;
}

export function setFileClickMode(mode: FileClickMode): void {
  currentMode = mode;
}

export function setFilePreviewListener(
  listener: ((request: FilePreviewRequest) => void) | null,
): void {
  previewListener = listener;
}

export function requestFilePreview(request: FilePreviewRequest): void {
  previewListener?.(request);
}

/** Local Markdown destinations, including Codex's /C:/... and file:/// forms. */
export function isLocalFileLink(href: string): boolean {
  return /^(?:file:\/\/|\/?[a-z]:[\\/]|\\\\|\/[^/])/i.test(href);
}

export async function revealLocalFile(path: string): Promise<void> {
  const response = await fetch("/api/reveal-local", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Не удалось открыть папку");
}
