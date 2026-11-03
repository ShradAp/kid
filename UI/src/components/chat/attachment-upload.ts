import { toast } from "sonner";
import type { Attachment } from "@/components/chat/input-deck";
import type { Persona } from "@/lib/assistant-data";

export async function extractVideoFrames(file: File, count = 3): Promise<string[]> {
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

export async function uploadAttachments(sessionAttachments: Attachment[], activePersona?: Persona) {
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
  return { accumulatedFilesPrefix, payloadAttachments, sentAttachments };
}
