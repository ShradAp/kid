import type { ReactNode } from "react";
import { toast } from "sonner";
import { ThinkingAccordion } from "@/components/chat/thinking-accordion";
import { MarketplaceCard } from "@/components/chat/marketplace-card";
import { Paperclip, Plus } from "lucide-react";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import type { Message } from "@/lib/assistant-data";
import { getFileClickMode, requestFilePreview } from "@/lib/file-actions";
import katex from "katex";
import "katex/dist/katex.min.css";

function splitTableRow(line: string): string[] {
  let t = line.trim();
  if (t.startsWith("|")) t = t.slice(1);
  if (t.endsWith("|")) t = t.slice(0, -1);
  return t.split("|").map((c) => c.trim());
}

function isTableSeparator(line: string): boolean {
  const cells = splitTableRow(line);
  if (cells.length === 0) return false;
  return cells.every((c) => /^:?-+:?$/.test(c));
}

function tableAlign(value: string | undefined): "left" | "center" | "right" {
  const s = (value ?? "").trim();
  if (s.startsWith(":") && s.endsWith(":") && s.length > 2) return "center";
  if (s.endsWith(":")) return "right";
  return "left";
}

function renderTable(
  header: string[],
  aligns: string[],
  rows: string[][],
  key: string,
) {
  return (
    <div
      key={key}
      className="my-2 overflow-x-auto rounded-lg border border-white/10"
    >
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="bg-white/5">
            {header.map((cell, i) => (
              <th
                key={i}
                style={{ textAlign: tableAlign(aligns[i]) }}
                className="border-b border-white/10 px-3 py-1.5 font-semibold text-foreground"
              >
                {formatInline(cell)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, r) => (
            <tr key={r} className="odd:bg-white/[0.02]">
              {header.map((_, i) => (
                <td
                  key={i}
                  style={{ textAlign: tableAlign(aligns[i]) }}
                  className="border-b border-white/5 px-3 py-1.5 text-foreground/90"
                >
                  {formatInline(row[i] ?? "")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function decodeMarkupText(value: string): string {
  return value
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;/gi, "'")
    .replace(/<[^>]*>/g, "");
}

function renderToolTrace(line: string, key: string | number) {
  const parts = line.split(/(<span\b[^>]*>.*?<\/span>|&nbsp;)/gi).filter(Boolean);
  return (
    <div
      key={`tool-trace-${key}`}
      className="my-1 flex flex-wrap items-start gap-1.5 font-mono text-[0.78em] leading-relaxed [overflow-wrap:anywhere] [word-break:normal]"
    >
      {parts.map((part, index) => {
        if (/^&nbsp;$/i.test(part)) return <span key={index}> </span>;
        const text = decodeMarkupText(part);
        if (!text) return null;
        const isTool = /search_code|execute_command|read_file|replace_in_file|write_file/.test(text);
        const isTime = /^\d{2}:\d{2}:\d{2}$/.test(text);
        const isLabel = /^(query:|in |↳)/.test(text);
        return (
          <span
            key={index}
            className={
              isTool
                ? "rounded bg-zinc-800 px-1.5 py-0.5 text-blue-400"
                : isTime
                  ? "text-zinc-500"
                  : isLabel
                    ? "text-zinc-400"
                    : "text-zinc-300"
            }
          >
            {text}
          </span>
        );
      })}
    </div>
  );
}

function renderMarkdown(content: string) {
  const lines = content.split("\n");
  const elements: ReactNode[] = [];
  let inCodeBlock = false;
  let codeBuffer: string[] = [];
  let codeKey: string | number = 0;
  let codeLang = "";
  let listBuffer: { ordered: boolean; items: string[] } | null = null;
  let quoteBuffer: string[] = [];
  let paraBuffer: string[] = [];
  let inDisplayMath = false;
  let displayMathBuffer: string[] = [];

  const flushPara = () => {
    if (paraBuffer.length === 0) return;
    const chunk = paraBuffer;
    paraBuffer = [];
    elements.push(
      <p key={`p-${elements.length}`} className="leading-relaxed text-foreground/90">
        {chunk.map((ln, li) => (
          <span key={li}>
            {li > 0 && <br />}
            {formatInline(ln)}
          </span>
        ))}
      </p>,
    );
  };

  const flushList = () => {
    if (!listBuffer) return;
    const { ordered, items } = listBuffer;
    listBuffer = null;
    const renderItem = (item: string, i: number) => {
      const task = item.match(/^\[([ xX])\]\s*(.*)$/);
      if (!ordered && task) {
        const checked = task[1].toLowerCase() === "x";
        return (
          <li key={i} className="flex items-start gap-2 pl-1">
            <input
              type="checkbox"
              checked={checked}
              readOnly
              className="mt-1 size-3.5 shrink-0 accent-primary"
            />
            <span className={checked ? "text-foreground/60 line-through" : undefined}>
              {formatInline(task[2])}
            </span>
          </li>
        );
      }
      return (
        <li key={i} className="pl-1">
          {formatInline(item)}
        </li>
      );
    };
    if (ordered) {
      elements.push(
        <ol
          key={`ol-${elements.length}`}
          className="ml-5 list-decimal space-y-0.5 text-foreground/90"
        >
          {items.map((item, i) => renderItem(item, i))}
        </ol>,
      );
    } else {
      elements.push(
        <ul
          key={`ul-${elements.length}`}
          className="ml-5 list-disc space-y-0.5 text-foreground/90"
        >
          {items.map((item, i) => renderItem(item, i))}
        </ul>,
      );
    }
  };

  const flushQuote = () => {
    if (quoteBuffer.length === 0) return;
    const items = quoteBuffer;
    quoteBuffer = [];
    elements.push(
      <blockquote
        key={`q-${elements.length}`}
        className="space-y-1 border-l-2 border-primary/50 pl-3 text-foreground/80"
      >
        {items.map((t, i) => (
          <p key={i} className="leading-relaxed">
            {formatInline(t)}
          </p>
        ))}
      </blockquote>,
    );
  };

  const pushCodeBlock = (key: string | number, lang: string) => {
    // Код выводится ЦЕЛИКОМ: без обрезки строк, без лимита строк,
    // без плашек «ещё N строк скрыто» — блоки никогда не сворачиваются.
    elements.push(
      <div
        key={`code-${key}`}
        className="my-2 overflow-hidden rounded-lg border border-white/10"
      >
        {lang && (
          <div className="border-b border-white/10 bg-white/5 px-3 py-1 font-mono text-[11px] text-muted-foreground">
            {lang}
          </div>
        )}
        <pre className="overflow-x-auto bg-black/40 p-3 font-mono text-xs leading-relaxed text-emerald-400 whitespace-pre-wrap [overflow-wrap:anywhere]">
          <code>{codeBuffer.join("\n")}</code>
        </pre>
      </div>,
    );
    codeBuffer = [];
  };

  const flushDisplayMath = () => {
    if (displayMathBuffer.length === 0) return;
    const tex = displayMathBuffer.join("\n");
    displayMathBuffer = [];
    elements.push(
      <div key={`math-${elements.length}`} className="my-3 overflow-x-auto">
        {renderMath(tex, true)}
      </div>,
    );
  };

  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    const trimmed = line.trim();

    if (trimmed.startsWith("```") || trimmed.startsWith("~~~")) {
      if (inCodeBlock) {
        pushCodeBlock(codeKey, codeLang);
        inCodeBlock = false;
        codeLang = "";
      } else {
        flushPara();
        flushList();
        flushQuote();
        inCodeBlock = true;
        codeKey = index;
        codeLang = trimmed.slice(3).trim().split(/\s+/)[0] ?? "";
      }
      continue;
    }

    if (inCodeBlock) {
      codeBuffer.push(line);
      continue;
    }

    // Многострочный блок $$ ... $$ — собираем целиком, не разбивая \n внутри.
    if (inDisplayMath) {
      if (trimmed === "$$") {
        inDisplayMath = false;
        flushDisplayMath();
      } else if (trimmed.endsWith("$$") && !trimmed.startsWith("$$")) {
        // Закрывающий $$ с хвостом контента: \end{cases} $$
        displayMathBuffer.push(trimmed.slice(0, -2));
        inDisplayMath = false;
        flushDisplayMath();
      } else {
        displayMathBuffer.push(line);
      }
      continue;
    }

    // Однострочный $$...$$
    if (/^\$\$.*\$\$$/.test(trimmed) && trimmed.length > 4) {
      flushPara();
      flushList();
      flushQuote();
      elements.push(
        <div key={`math-${elements.length}`} className="my-3 overflow-x-auto">
          {renderMath(trimmed.slice(2, -2).trim(), true)}
        </div>,
      );
      continue;
    }

    // Открытие многострочного блока: $$ (отдельной строкой) или $$... на первой
    if (trimmed.startsWith("$$")) {
      flushPara();
      flushList();
      flushQuote();
      inDisplayMath = true;
      const rest = trimmed.slice(2);
      if (rest.trim()) displayMathBuffer.push(rest);
      continue;
    }

    if (/<span\b[^>]*>|&nbsp;/i.test(line)) {
      flushPara();
      flushList();
      flushQuote();
      elements.push(renderToolTrace(line, index));
      continue;
    }

    // GFM-таблица: строка заголовка + строка-разделитель
    if (
      line.includes("|") &&
      index + 1 < lines.length &&
      isTableSeparator(lines[index + 1])
    ) {
      flushPara();
      flushList();
      flushQuote();
      const header = splitTableRow(line);
      const aligns = splitTableRow(lines[index + 1]);
      const rows: string[][] = [];
      let j = index + 2;
      while (
        j < lines.length &&
        lines[j].includes("|") &&
        lines[j].trim() !== ""
      ) {
        rows.push(splitTableRow(lines[j]));
        j++;
      }
      elements.push(renderTable(header, aligns, rows, `tbl-${index}`));
      index = j - 1;
      continue;
    }

    // Горизонтальная линия
    if (/^\s*(---|\*\*\*|___)\s*$/.test(line)) {
      flushPara();
      flushList();
      flushQuote();
      elements.push(<hr key={index} className="my-3 border-white/10" />);
      continue;
    }

    // Заголовки
    const hMatch = line.match(/^(#{1,6})\s+(.*)$/);
    if (hMatch) {
      flushPara();
      flushList();
      flushQuote();
      const level = hMatch[1].length;
      const text = hMatch[2];
      const cls =
        level === 1
          ? "mb-2 mt-4 text-xl font-bold text-foreground"
          : level === 2
            ? "mb-1 mt-3 text-lg font-bold text-foreground"
            : "mb-1 mt-3 text-base font-bold text-foreground";
      const Tag = `h${Math.min(level, 6)}` as
        | "h1"
        | "h2"
        | "h3"
        | "h4"
        | "h5"
        | "h6";
      elements.push(
        <Tag key={index} className={cls}>
          {formatInline(text)}
        </Tag>,
      );
      continue;
    }

    // Цитата
    const qMatch = line.match(/^\s*>\s?(.*)$/);
    if (qMatch) {
      flushPara();
      flushList();
      quoteBuffer.push(qMatch[1]);
      continue;
    }
    if (quoteBuffer.length > 0) flushQuote();

    // Нумерованный список
    const olMatch = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (olMatch) {
      flushPara();
      if (quoteBuffer.length > 0) flushQuote();
      if (!listBuffer || !listBuffer.ordered) {
        flushList();
        listBuffer = { ordered: true, items: [] };
      }
      listBuffer.items.push(olMatch[1]);
      continue;
    }

    // Маркированный список (+ task-list `- [ ]` / `- [x]`)
    const ulMatch = line.match(/^\s*[-*+•]\s+(.*)$/);
    if (ulMatch) {
      flushPara();
      if (quoteBuffer.length > 0) flushQuote();
      if (!listBuffer || listBuffer.ordered) {
        flushList();
        listBuffer = { ordered: false, items: [] };
      }
      listBuffer.items.push(ulMatch[1]);
      continue;
    }
    if (listBuffer) flushList();

    // Пустые строки как отступы
    if (!line.trim()) {
      flushPara();
      flushList();
      if (quoteBuffer.length > 0) flushQuote();
      elements.push(<div key={index} className="h-2" />);
      continue;
    }

    // Обычный абзац: склеиваем соседние строки в один <p> через <br/>
    if (listBuffer) flushList();
    if (quoteBuffer.length > 0) flushQuote();
    paraBuffer.push(line);
  }

  if (inCodeBlock) pushCodeBlock(`unclosed-${lines.length}`, codeLang);
  if (inDisplayMath) flushDisplayMath();
  flushPara();
  flushList();
  flushQuote();

  return elements;
}

// ---------------------------------------------------------------------------
// Рендеринг математики (KaTeX)
// ---------------------------------------------------------------------------

// Команды, которые распознаём как «голую» формулу (без $...$). Список ограничен
// реальными LaTeX-командами, чтобы не зацепить случайные Windows-пути (\Users и т.п.).
const BARE_MATH_COMMANDS = [
  "frac", "dfrac", "tfrac", "cfrac", "binom", "dbinom", "tbinom",
  "sqrt", "sum", "prod", "int", "oint", "iint", "iiint", "lim", "liminf", "limsup",
  "log", "ln", "lg", "sin", "cos", "tan", "cot", "sec", "csc",
  "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh", "coth",
  "exp", "max", "min", "sup", "inf", "det", "gcd", "deg", "dim", "hom", "ker",
  "pm", "mp", "times", "div", "cdot", "ast", "star", "circ", "bullet",
  "cap", "cup", "vee", "wedge", "oplus", "ominus", "otimes", "oslash", "odot",
  "le", "ge", "leq", "geq", "ne", "neq", "equiv", "approx", "sim", "simeq", "cong", "propto",
  "in", "ni", "subset", "supset", "subseteq", "supseteq",
  "forall", "exists", "nexists", "neg", "land", "lor", "lnot",
  "to", "rightarrow", "leftarrow", "leftrightarrow", "Rightarrow", "Leftarrow", "Leftrightarrow",
  "mapsto", "implies", "iff", "Longrightarrow",
  "nabla", "partial", "infty", "aleph", "hbar", "ell", "Re", "Im",
  "angle", "triangle", "square", "Box", "Diamond",
  "text", "textbf", "textit", "textrm", "textsf", "texttt",
  "mathrm", "mathbf", "mathit", "mathsf", "mathtt", "mathbb", "mathcal", "mathfrak",
  "hat", "bar", "vec", "dot", "ddot", "tilde", "widehat", "widetilde",
  "overline", "underline", "overrightarrow", "overleftarrow", "underbrace", "overbrace",
  "prime", "degree", "colon", "ldots", "cdots", "vdots", "ddots", "dots",
  "alpha", "beta", "gamma", "delta", "epsilon", "varepsilon", "zeta", "eta",
  "theta", "vartheta", "iota", "kappa", "lambda", "mu", "nu", "xi", "omicron",
  "pi", "varpi", "rho", "varrho", "sigma", "varsigma", "tau", "upsilon",
  "phi", "varphi", "chi", "psi", "omega",
  "Gamma", "Delta", "Theta", "Lambda", "Xi", "Pi", "Sigma", "Upsilon", "Phi", "Psi", "Omega",
];

// Сортируем по длине по убыванию, чтобы \dfrac не матчился как \frac.
const BARE_MATH_COMMAND_SOURCE = BARE_MATH_COMMANDS
  .slice()
  .sort((a, b) => b.length - a.length)
  .join("|");

// «Голая» формула: \команда + сразу примыкающие математические символы
// (цифры, скобки, операторы). String.raw, чтобы не экранировать слэши вручную.
const BARE_MATH_RE = new RegExp(
  String.raw`(\\%|\\(?:${BARE_MATH_COMMAND_SOURCE})[A-Za-z0-9.,+\-*/^_=<>()\[\]{}]*)`,
  "g",
);

// Приводит «сырой» LaTeX с бэкенда/ИИ к виду, который корректно понимает KaTeX:
//  1. Схлопывает дублированные обратные слэши (артефакт JSON-эскейпинга).
//  2. Убирает принудительное экранирование процента, но сохраняет корректный
//     LaTeX-эскейп: голый % в LaTeX — комментарий, поэтому % -> \%.
//  3. Чинит «голые» дроби без фигурных скобок: \frac190,0025 -> \frac{190}{0025}.
function normalizeLatex(latex: string): string {
  let s = latex;

  // 1) Схлопываем дублированные команды (\\frac, \\%, \\begin и т.п.), но
  //    НЕ трогаем переносы строк \\ внутри окружений (cases/pmatrix/align):
  //    после переноса \\ идёт пробел/перевод строки, а не буква/команда.
  s = s.replace(/\\\\(?=[A-Za-z%{_^])/g, "\\");

  // 2) процент: % (не после слэша) -> \%; уже экранированный \% не трогаем.
  s = s.replace(/(^|[^\\])%/g, "$1\\%");

  // 3) \frac / \dfrac / \tfrac / \cfrac без фигурных скобок.
  //    Корректные \frac{1}{2} и \frac12 (два одиночных токена) не затрагиваются.
  s = s.replace(
    /\\(frac|dfrac|tfrac|cfrac)[ \t]*([0-9][0-9.]*)[ \t]*([,/])[ \t]*([0-9][0-9.]*)/g,
    (_m, cmd: string, a: string, _sep: string, b: string) => `\\${cmd}{${a}}{${b}}`,
  );

  return s;
}

function renderMath(latex: string, display = false) {
  const normalized = normalizeLatex(latex);
  try {
    const html = katex.renderToString(normalized, {
      displayMode: display,
      throwOnError: false,
      strict: false,
      output: "html",
    });
    return (
      <span
        className={
          display
            ? "my-2 block overflow-x-auto text-center"
            : "inline-block align-middle"
        }
        dangerouslySetInnerHTML={{ __html: html }}
      />
    );
  } catch {
    // KaTeX не справился — показываем нормализованный текст, но не сырьё.
    return <span className="font-serif italic">{normalized}</span>;
  }
}

// Рендерит «голую» математику внутри обычного текста (без $...$): находит
// \frac..., \%, \sqrt... и т.п. и превращает их в формулы через KaTeX.
function renderPlainInline(text: string, key: string | number) {
  if (!text.includes("\\")) return <span key={key}>{text}</span>;

  const parts = text.split(BARE_MATH_RE);
  if (parts.length === 1) return <span key={key}>{text}</span>;

  return (
    <span key={key}>
      {parts.map((part, i) => {
        if (!part) return null;
        // Нечётный индекс — захваченная «голая» формула.
        if (i % 2 === 1) return <span key={i}>{renderMath(part)}</span>;
        return <span key={i}>{part}</span>;
      })}
    </span>
  );
}

async function downloadGeneratedFile(fileId: string, filename: string) {
  // 1) Нативное «Сохранить как» через бэкенд — надёжно в WebView2, где
  //    скриптовое скачивание blob-ссылки может молча игнорироваться.
  try {
    const res = await fetch("/api/save-file", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file_id: fileId, name: filename }),
    });
    const data = (await res.json().catch(() => ({}))) as {
      error?: string;
      path?: string;
      auto?: boolean;
    };
    if (res.ok) {
      if (data.auto && data.path) {
        toast.success("Файл сохранён", { description: data.path });
      }
      return; // сохранено либо диалог отменён пользователем
    }
    // 404 — старый мост без /api/save-file; пробуем скачивание браузером ниже.
    if (res.status !== 404) {
      console.error("Не удалось сохранить файл:", data.error || `HTTP ${res.status}`);
      return;
    }
  } catch (error) {
    console.error("Не удалось сохранить файл:", error);
    return;
  }
  // 2) Фолбэк: blob-скачивание через объектную ссылку (обычный браузер).
  try {
    const res = await fetch(
      `/api/generated/${encodeURIComponent(fileId)}`,
    );
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const objectUrl = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = filename || "file";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 2000);
  } catch (error) {
    console.error("Не удалось скачать файл:", error);
  }
}

function formatInline(text: string) {
  const parts = text.split(
    /(\$\$[^$]+\$\$|\$[^$]+\$|`[^`]+`|\[[^\]]+\]\([^\s]+(?:\s+"[^"]*")?\)|https?:\/\/[^\s<>"')\]]+|www\.[^\s<>"')\]]+|\*\*[^*]+\*\*|~~[^~]+~~|\*[^*]+\*)/g,
  );
  return parts.map((part, i) => {
    if (!part) return null;
    if (part.startsWith("$$") && part.endsWith("$$") && part.length > 4) {
      return <span key={i}>{renderMath(part.slice(2, -2), true)}</span>;
    }
    if (part.startsWith("$") && part.endsWith("$") && part.length > 2) {
      return <span key={i}>{renderMath(part.slice(1, -1))}</span>;
    }
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      return (
        <code
          key={i}
          className="rounded bg-white/10 px-1 py-0.5 font-mono text-xs text-amber-300"
        >
          {part.slice(1, -1)}
        </code>
      );
    }
    const linkMatch = part.match(
      /^\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)$/,
    );
    if (linkMatch) {
      const href = linkMatch[2];
      // Ссылки rab://download/<id> — файлы, созданные моделью. ОС не умеет
      // открывать протокол rab://, поэтому вместо обычной ссылки показываем
      // иконку-скрепку, а скачивание идёт через мост бэкенда.
      if (href.startsWith("rab://download/")) {
        const fileId = href.slice("rab://download/".length);
        const label = linkMatch[1];
        const nameMatch = label.match(/«([^»]+)»/);
        const filename =
          nameMatch?.[1] ||
          label.replace(/^Скачать файл\s*/, "").trim() ||
          "file";
        return (
          <Tooltip key={i}>
            <TooltipTrigger
              render={
                <a
                  href={`/api/generated/${encodeURIComponent(fileId)}`}
                  download={filename}
                  aria-label={`Скачать ${filename}`}
                  onClick={(event) => {
                    event.preventDefault();
                    if (getFileClickMode() === "preview") {
                      requestFilePreview({ fileId, filename });
                    } else {
                      void downloadGeneratedFile(fileId, filename);
                    }
                  }}
                  className="inline-flex items-center rounded p-0.5 align-middle text-primary transition-colors hover:bg-primary/15 hover:text-primary"
                />
              }
            >
              <Paperclip className="size-3.5" />
            </TooltipTrigger>
            <TooltipContent>{filename}</TooltipContent>
          </Tooltip>
        );
      }
      return (
        <a
          key={i}
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          className="break-all text-primary underline underline-offset-2 hover:opacity-80"
        >
          {formatInline(linkMatch[1])}
        </a>
      );
    }
    if (
      /^https?:\/\/[^\s<>"')\]]+$/.test(part) ||
      /^www\.[^\s<>"')\]]+$/.test(part)
    ) {
      const trailMatch = part.match(/^(.*?)([.,;:!?)\]]+)$/);
      const url = trailMatch ? trailMatch[1] : part;
      const trail = trailMatch ? trailMatch[2] : "";
      const href = url.startsWith("www.") ? `https://${url}` : url;
      return (
        <span key={i}>
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="break-all text-primary underline underline-offset-2 hover:opacity-80"
          >
            {url}
          </a>
          {trail}
        </span>
      );
    }
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      return (
        <strong key={i} className="font-semibold text-foreground">
          {formatInline(part.slice(2, -2))}
        </strong>
      );
    }
    if (part.startsWith("~~") && part.endsWith("~~") && part.length > 4) {
      return (
        <span key={i} className="line-through text-foreground/80">
          {formatInline(part.slice(2, -2))}
        </span>
      );
    }
    if (part.startsWith("*") && part.endsWith("*") && part.length > 2) {
      return (
        <em key={i} className="italic text-foreground/80">
          {formatInline(part.slice(1, -1))}
        </em>
      );
    }
    return renderPlainInline(part, i);
  });
}

function MessageAttachments({
  attachments,
}: {
  attachments: NonNullable<Message["attachments"]>;
}) {
  const visibleAttachments = attachments.slice(0, 5);
  const hiddenAttachments = attachments.slice(5);

  return (
    <span className="flex items-center gap-0.5">
      {visibleAttachments.map((attachment, index) => (
        <Tooltip key={`${attachment.name}-${index}`}>
          <TooltipTrigger
            render={
              <button
                type="button"
                onClick={() =>
                  requestFilePreview({
                    filename: attachment.name,
                    url: attachment.url,
                  })
                }
                aria-label={`Предпросмотр ${attachment.name}`}
                className="rounded p-0.5 transition-colors hover:bg-primary/15 hover:text-primary"
              />
            }
          >
            <Paperclip className="size-3.5" />
          </TooltipTrigger>
          <TooltipContent>{attachment.name}</TooltipContent>
        </Tooltip>
      ))}
      {hiddenAttachments.length > 0 && (
        <Tooltip>
          <TooltipTrigger
            render={
              <button
                type="button"
                aria-label={`Ещё файлов: ${hiddenAttachments.length}`}
                className="rounded p-0.5 transition-colors hover:bg-primary/15 hover:text-primary"
              />
            }
          >
            <Plus className="size-3.5" />
          </TooltipTrigger>
          <TooltipContent>
            <span className="flex max-w-64 flex-col gap-0.5">
              {hiddenAttachments.map((attachment, index) => (
                <span key={`${attachment.name}-${index}`}>{attachment.name}</span>
              ))}
            </span>
          </TooltipContent>
        </Tooltip>
      )}
    </span>
  );
}

export function ChatMessage({ message }: { message: Message }) {
  if (message.role === "user") {
    return (
      <div className="ml-auto flex max-w-[80%] flex-col items-end gap-1 select-text">
        <div className="whitespace-pre-wrap rounded-2xl bg-primary/20 border border-primary/30 px-4 py-2.5 text-right text-[1em] leading-relaxed text-foreground select-text cursor-text">
          {message.content}
        </div>
        <div className="flex items-center gap-2 text-[0.7em] text-muted-foreground">
          <span>{message.timestamp}</span>
          {message.attachments && message.attachments.length > 0 && (
            <MessageAttachments attachments={message.attachments} />
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="flex max-w-[85%] flex-col gap-1 select-text">
      <div className="rounded-2xl border border-white/10 bg-card p-4 space-y-1 select-text cursor-text">
        {(message.thinking || message.reasoning) && (
          <ThinkingAccordion
            steps={message.thinking ?? []}
            reasoning={message.reasoning}
          />
        )}
        <div className="select-text cursor-text">
          {renderMarkdown(message.content)}
        </div>
        {message.marketplaceProduct && (
          <div className="mt-3">
            <MarketplaceCard product={message.marketplaceProduct} />
          </div>
        )}
      </div>
      <span className="ml-1 text-[0.7em] text-muted-foreground">
        {message.timestamp}
      </span>
    </div>
  );
}
