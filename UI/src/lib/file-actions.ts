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
