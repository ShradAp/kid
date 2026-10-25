"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, Minus, Plus } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import {
  type StudioPreset,
  type StudioPreview,
  type StudioPreviewBlock,
} from "@/lib/studio-data";

/** Формат бумаги A4 в миллиметрах — физический размер листа, не оформление. */
const A4_SHORT_MM = 210;
const A4_LONG_MM = 297;
/** CSS-пиксели на миллиметр при 96 dpi. */
const MM_TO_PX = 96 / 25.4;

const MIN_ZOOM = 0.4;
const MAX_ZOOM = 1.6;
const ZOOM_STEP = 0.1;
const DEFAULT_ZOOM = 0.72;

const DEFAULT_PAGE = {
  top: 20,
  bottom: 20,
  left: 30,
  right: 15,
  orientation: "portrait" as const,
};

function clampZoom(value: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Math.round(value * 100) / 100));
}

function alignmentToTextAlign(value?: string | null): "left" | "center" | "right" | "justify" {
  if (value === "center" || value === "right" || value === "left" || value === "justify") {
    return value;
  }
  return "left";
}

/**
 * Центральный холст: тёмная рабочая зона и белый лист A4. Рендерится реальное
 * содержимое прикреплённого DOCX (модель предпросмотра с бэкенда). Пока файл не
 * прикреплён — лист пуст. Ответы модели сюда не попадают: документ меняется
 * только инструментами и явным применением пресета/правил.
 */
export function DocumentA4Canvas({
  preset,
  preview,
}: {
  preset: StudioPreset | null;
  preview: StudioPreview | null;
}) {
  const [zoom, setZoom] = useState(DEFAULT_ZOOM);
  const [pageIndex, setPageIndex] = useState(0);
  const [contentHeight, setContentHeight] = useState(0);
  const contentRef = useRef<HTMLDivElement>(null);

  const page = preview?.page ?? preset?.page ?? DEFAULT_PAGE;
  const landscape = page.orientation === "landscape";
  const widthMm = landscape ? A4_LONG_MM : A4_SHORT_MM;
  const heightMm = landscape ? A4_SHORT_MM : A4_LONG_MM;
  const pageWidthPx = widthMm * MM_TO_PX;
  const pageHeightPx = heightMm * MM_TO_PX;

  const { top, bottom, left, right } = page;

  const contentBoxPx = useMemo(
    () => Math.max(1, (heightMm - top - bottom) * MM_TO_PX),
    [heightMm, top, bottom],
  );

  const pageCount = Math.max(1, Math.ceil(contentHeight / contentBoxPx));

  useEffect(() => {
    const element = contentRef.current;
    if (!element) return;
    const measure = () => setContentHeight(element.scrollHeight);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [preview, preset]);

  useEffect(() => {
    setPageIndex((current) => Math.min(current, pageCount - 1));
  }, [pageCount]);

  const bodyFont = preset?.body;

  function renderPreviewBlock(block: StudioPreviewBlock, index: number) {
    const fontSizePt = block.fontSizePt ?? bodyFont?.font_size ?? 14;
    const fontName = block.fontName ?? bodyFont?.font_name ?? "Times New Roman";
    const lineSpacing = block.lineSpacing ?? bodyFont?.line_spacing ?? 1.5;
    const alignment = alignmentToTextAlign(block.alignment ?? bodyFont?.alignment);
    const firstLine = block.firstLineIndentMm ?? bodyFont?.first_line_indent ?? 0;
    const style = {
      fontFamily: `"${fontName}", Times, serif`,
      fontSize: `${fontSizePt}pt`,
      fontWeight: block.bold ? 700 : 400,
      fontStyle: block.italic ? "italic" : "normal",
      textAlign: alignment,
      lineHeight: lineSpacing,
      textIndent: `${firstLine}mm`,
      marginTop: `${block.spaceBeforePt ?? 0}pt`,
      marginBottom: `${block.spaceAfterPt ?? bodyFont?.space_after ?? 0}pt`,
    } as const;

    if (block.type === "table") {
      const tableAlign = block.tableAlignment;
      return (
        <div
          key={`tbl_${index}`}
          className={
            tableAlign === "center" ? "mx-auto w-fit" : tableAlign === "right" ? "ml-auto w-fit" : "w-fit"
          }
          style={{
            marginTop: `${bodyFont?.space_before ?? 6}pt`,
            marginBottom: `${bodyFont?.space_after ?? 6}pt`,
          }}
        >
          <table className="border-collapse" style={{ fontSize: `${block.fontSizePt ?? 12}pt` }}>
            <tbody>
              {(block.rows ?? []).map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {row.map((cell, cellIndex) => (
                    <td
                      key={cellIndex}
                      className="border border-black px-2 py-1 align-top"
                      style={{ fontFamily: `"${block.fontName ?? "Times New Roman"}", serif` }}
                    >
                      {cell}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    }

    if (block.type === "image" && block.dataUrl) {
      return (
        <div key={`img_${index}`} style={{ textAlign: alignment }}>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={block.dataUrl}
            alt={block.text ?? "Рисунок"}
            style={{
              display: "inline-block",
              width: block.widthMm ? `${block.widthMm}mm` : undefined,
              maxWidth: "100%",
              marginTop: `${block.spaceBeforePt ?? 0}pt`,
              marginBottom: `${block.spaceAfterPt ?? 0}pt`,
            }}
          />
        </div>
      );
    }

    return (
      <p key={`p_${index}`} style={style}>
        {block.text}
      </p>
    );
  }



  const hasPreview = !!preview && preview.blocks.length > 0;

  return (
    <section className="relative flex min-w-0 flex-1 flex-col bg-background">
      <div className="relative flex-1 overflow-auto">
        <div className="flex min-h-full justify-center px-6 py-8">
          <div
            className="relative shrink-0"
            style={{ width: pageWidthPx * zoom, height: pageHeightPx * zoom }}
          >
            <div
              className="absolute left-0 top-0 origin-top-left bg-white text-black shadow-2xl"
              style={{
                width: pageWidthPx,
                height: pageHeightPx,
                transform: `scale(${zoom})`,
              }}
            >
              <div
                className="h-full w-full"
                style={{
                  paddingTop: `${top}mm`,
                  paddingBottom: `${bottom}mm`,
                  paddingLeft: `${left}mm`,
                  paddingRight: `${right}mm`,
                }}
              >
                <div className="relative h-full w-full overflow-hidden">
                  <div
                    ref={contentRef}
                    style={{ transform: `translateY(${-pageIndex * contentBoxPx}px)` }}
                  >
                    {hasPreview && preview!.blocks.map(renderPreviewBlock)}
                    {!hasPreview && (
                      <p className="py-16 text-center text-sm text-neutral-400">
                        Прикрепите .docx к студии — здесь появится его предпросмотр.
                      </p>
                    )}
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* Панель навигации — тот же плавающий паттерн, что и у InputDeck. */}
        <div className="pointer-events-none sticky bottom-0 z-20 px-4 pb-4 pt-2">
          <div className="pointer-events-auto mx-auto flex w-fit items-center gap-1 rounded-2xl border border-white/10 bg-card/95 p-1.5 shadow-2xl shadow-black/40 backdrop-blur-md">
            <Button
              variant="ghost"
              size="icon-sm"
              className="rounded-full"
              onClick={() => setPageIndex((value) => Math.max(0, value - 1))}
              disabled={pageIndex <= 0}
              aria-label="Предыдущая страница"
            >
              <ChevronLeft />
            </Button>
            <Badge variant="secondary" className="h-5 px-2">
              {pageIndex + 1} / {pageCount}
            </Badge>
            <Button
              variant="ghost"
              size="icon-sm"
              className="rounded-full"
              onClick={() => setPageIndex((value) => Math.min(pageCount - 1, value + 1))}
              disabled={pageIndex >= pageCount - 1}
              aria-label="Следующая страница"
            >
              <ChevronRight />
            </Button>

            <Separator orientation="vertical" className="mx-1 h-5" />

            <Button
              variant="ghost"
              size="icon-sm"
              className="rounded-full"
              onClick={() => setZoom((value) => clampZoom(value - ZOOM_STEP))}
              disabled={zoom <= MIN_ZOOM}
              aria-label="Уменьшить масштаб"
            >
              <Minus />
            </Button>
            <Button
              variant="ghost"
              size="sm"
              className="tabular-nums"
              onClick={() => setZoom(DEFAULT_ZOOM)}
              title="Сбросить масштаб"
            >
              {Math.round(zoom * 100)}%
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              className="rounded-full"
              onClick={() => setZoom((value) => clampZoom(value + ZOOM_STEP))}
              disabled={zoom >= MAX_ZOOM}
              aria-label="Увеличить масштаб"
            >
              <Plus />
            </Button>
          </div>
        </div>
      </div>
    </section>
  );
}
