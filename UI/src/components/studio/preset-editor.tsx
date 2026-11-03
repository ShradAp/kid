"use client";

import { Field, FieldContent, FieldLabel, FieldSeparator, FieldTitle } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { type StudioPreset } from "@/lib/studio-data";

const ALIGN_LABELS: Record<string, string> = {
  left: "по левому краю",
  center: "по центру",
  right: "по правому краю",
  justify: "по ширине",
};

function toNumber(value: string): number {
  const parsed = Number(value.replace(",", "."));
  return Number.isFinite(parsed) ? parsed : 0;
}

/** Обновляет поле одной секции пресета, не мутируя исходный объект. */
function withSection<K extends "page" | "body" | "tables" | "captions" | "figures" | "lists">(
  preset: StudioPreset,
  key: K,
  patch: Partial<StudioPreset[K]>,
): StudioPreset {
  return { ...preset, [key]: { ...preset[key], ...patch } };
}

/** Обновляет настройки одного уровня заголовка. */
function withHeading(
  preset: StudioPreset,
  level: string,
  patch: Partial<StudioPreset["headings"][string]>,
): StudioPreset {
  const current = preset.headings[level];
  if (!current) return preset;
  return {
    ...preset,
    headings: { ...preset.headings, [level]: { ...current, ...patch } },
  };
}

/** Обновляет подблок титульного листа (header/title/signatures/footer). */
function withTitlePageBlock(
  preset: StudioPreset,
  block: "header" | "title" | "signatures" | "footer",
  patch: Record<string, unknown>,
): StudioPreset {
  const tp = preset.title_page;
  if (!tp) return preset;
  const current = tp[block] as unknown as Record<string, unknown>;
  return {
    ...preset,
    title_page: { ...tp, [block]: { ...current, ...patch } },
  } as StudioPreset;
}

/** Обновляет верхнеуровневые поля титульного листа. */
function withTitlePage(
  preset: StudioPreset,
  patch: Partial<NonNullable<StudioPreset["title_page"]>>,
): StudioPreset {
  const tp = preset.title_page;
  if (!tp) return preset;
  return { ...preset, title_page: { ...tp, ...patch } };
}

/** Обновляет поле специальных разделов (реферат/задание/содержание). */
function withSpecialSections(
  preset: StudioPreset,
  patch: Partial<NonNullable<StudioPreset["special_sections"]>>,
): StudioPreset {
  const ss = preset.special_sections;
  if (!ss) return preset;
  return { ...preset, special_sections: { ...ss, ...patch } };
}

/** Обновляет поле нумерации страниц. */
function withPageNumbering(
  preset: StudioPreset,
  patch: Partial<NonNullable<StudioPreset["page_numbering"]>>,
): StudioPreset {
  const pn = preset.page_numbering;
  if (!pn) return preset;
  return { ...preset, page_numbering: { ...pn, ...patch } };
}

function NumberField({
  label,
  value,
  onChange,
  step = 0.5,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  step?: number;
}) {
  return (
    <Field>
      <FieldLabel>{label}</FieldLabel>
      <Input
        type="number"
        step={step}
        value={Number.isFinite(value) ? value : 0}
        onChange={(event) => onChange(toNumber(event.target.value))}
      />
    </Field>
  );
}

function TextField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <Field>
      <FieldLabel>{label}</FieldLabel>
      <Input value={value ?? ""} onChange={(event) => onChange(event.target.value)} />
    </Field>
  );
}

function AlignmentField({
  label,
  value,
  onChange,
  allowInherit = false,
}: {
  label: string;
  value: string | null;
  onChange: (value: string) => void;
  allowInherit?: boolean;
}) {
  const items: Record<string, string> = { ...ALIGN_LABELS };
  if (allowInherit) items.inherit = "как в файле";
  const current = value && value !== "" ? value : allowInherit ? "inherit" : "left";
  return (
    <Field>
      <FieldLabel>{label}</FieldLabel>
      <Select
        value={current}
        onValueChange={(next) => onChange(String(next) === "inherit" ? "" : String(next))}
        items={items}
      >
        <SelectTrigger className="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {Object.entries(items).map(([key, text]) => (
            <SelectItem key={key} value={key}>
              {text}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </Field>
  );
}

function ToggleRow({
  title,
  checked,
  onChange,
}: {
  title: string;
  checked: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <Field orientation="horizontal">
      <FieldContent>
        <FieldTitle>{title}</FieldTitle>
      </FieldContent>
      <Switch checked={checked} onCheckedChange={onChange} />
    </Field>
  );
}

export function PresetEditor({ draft, onChangeDraft }: {
  draft: StudioPreset;
  onChangeDraft: (draft: StudioPreset) => void;
}) {
  return (
    <>
      <FieldSeparator>Поля страницы, мм</FieldSeparator>
      <div className="grid grid-cols-2 gap-3">
        <NumberField
          label="Верхнее"
          value={draft.page.top}
          onChange={(v) => onChangeDraft(withSection(draft, "page", { top: v }))}
        />
        <NumberField
          label="Нижнее"
          value={draft.page.bottom}
          onChange={(v) => onChangeDraft(withSection(draft, "page", { bottom: v }))}
        />
        <NumberField
          label="Левое"
          value={draft.page.left}
          onChange={(v) => onChangeDraft(withSection(draft, "page", { left: v }))}
        />
        <NumberField
          label="Правое"
          value={draft.page.right}
          onChange={(v) => onChangeDraft(withSection(draft, "page", { right: v }))}
        />
      </div>
      <Field>
        <FieldLabel>Ориентация</FieldLabel>
        <Select
          value={draft.page.orientation}
          onValueChange={(value) =>
            onChangeDraft(
              withSection(draft, "page", {
                orientation: value === "landscape" ? "landscape" : "portrait",
              }),
            )
          }
          items={{ portrait: "книжная", landscape: "альбомная" }}
        >
          <SelectTrigger className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="portrait">книжная</SelectItem>
            <SelectItem value="landscape">альбомная</SelectItem>
          </SelectContent>
        </Select>
      </Field>
      
      <FieldSeparator>Основной текст</FieldSeparator>
      <TextField
        label="Гарнитура"
        value={draft.body.font_name}
        onChange={(v) => onChangeDraft(withSection(draft, "body", { font_name: v }))}
      />
      <div className="grid grid-cols-2 gap-3">
        <NumberField
          label="Кегль, pt"
          value={draft.body.font_size}
          onChange={(v) => onChangeDraft(withSection(draft, "body", { font_size: v }))}
        />
        <NumberField
          label="Интервал, крат."
          value={draft.body.line_spacing}
          onChange={(v) => onChangeDraft(withSection(draft, "body", { line_spacing: v }))}
        />
        <NumberField
          label="До, pt"
          value={draft.body.space_before}
          onChange={(v) => onChangeDraft(withSection(draft, "body", { space_before: v }))}
        />
        <NumberField
          label="После, pt"
          value={draft.body.space_after}
          onChange={(v) => onChangeDraft(withSection(draft, "body", { space_after: v }))}
        />
        <NumberField
          label="Отступ 1-й строки, мм"
          value={draft.body.first_line_indent}
          onChange={(v) => onChangeDraft(withSection(draft, "body", { first_line_indent: v }))}
        />
      </div>
      <AlignmentField
        label="Выравнивание"
        value={draft.body.alignment}
        onChange={(v) => onChangeDraft(withSection(draft, "body", { alignment: v }))}
      />
      
      
      {(["h1", "h2", "h3"] as const)
        .filter((level) => draft.headings[level])
        .map((level) => {
          const heading = draft.headings[level];
          return (
            <div key={level} className="flex flex-col gap-3">
              <FieldSeparator>Заголовки {level.toUpperCase()}</FieldSeparator>
              <div className="grid grid-cols-2 gap-3">
                <NumberField
                  label="Кегль, pt"
                  value={heading.font_size}
                  onChange={(v) => onChangeDraft(withHeading(draft, level, { font_size: v }))}
                />
                <NumberField
                  label="До, pt"
                  value={heading.space_before}
                  onChange={(v) =>
                    onChangeDraft(withHeading(draft, level, { space_before: v }))
                  }
                />
                <NumberField
                  label="После, pt"
                  value={heading.space_after}
                  onChange={(v) =>
                    onChangeDraft(withHeading(draft, level, { space_after: v }))
                  }
                />
                <NumberField
                  label="Отступ, мм"
                  value={heading.first_line_indent}
                  onChange={(v) =>
                    onChangeDraft(withHeading(draft, level, { first_line_indent: v }))
                  }
                />
              </div>
              <AlignmentField
                label="Выравнивание"
                value={heading.alignment}
                onChange={(v) => onChangeDraft(withHeading(draft, level, { alignment: v }))}
              />
              <ToggleRow
                title="Полужирный"
                checked={heading.bold}
                onChange={(v) => onChangeDraft(withHeading(draft, level, { bold: v }))}
              />
            </div>
          );
        })}
      
      
      {draft.title_page && (
        <>
          <FieldSeparator>Титульный лист</FieldSeparator>
          <ToggleRow
            title="Оформлять титульный лист отдельно"
            checked={draft.title_page.enabled}
            onChange={(v) => onChangeDraft(withTitlePage(draft, { enabled: v }))}
          />
          <ToggleRow
            title="Отдельная секция Word (не наследовать оформление)"
            checked={draft.title_page.own_section}
            onChange={(v) => onChangeDraft(withTitlePage(draft, { own_section: v }))}
          />
          <ToggleRow
            title="Распределять блоки по вертикали (подвал к низу)"
            checked={draft.title_page.distribute_vertically}
            onChange={(v) =>
              onChangeDraft(withTitlePage(draft, { distribute_vertically: v }))
            }
          />
          <AlignmentField
            label="Шапка: выравнивание"
            value={draft.title_page.header.alignment}
            onChange={(v) =>
              onChangeDraft(withTitlePageBlock(draft, "header", { alignment: v }))
            }
          />
          <AlignmentField
            label="Заголовок работы: выравнивание"
            value={draft.title_page.title.alignment}
            onChange={(v) =>
              onChangeDraft(withTitlePageBlock(draft, "title", { alignment: v }))
            }
          />
          <ToggleRow
            title="Заголовок работы полужирным"
            checked={draft.title_page.title.bold}
            onChange={(v) =>
              onChangeDraft(withTitlePageBlock(draft, "title", { bold: v }))
            }
          />
          <div className="grid grid-cols-2 gap-3">
            <NumberField
              label="Заголовок: до, pt"
              value={draft.title_page.title.space_before}
              onChange={(v) =>
                onChangeDraft(withTitlePageBlock(draft, "title", { space_before: v }))
              }
            />
            <NumberField
              label="Заголовок: после, pt"
              value={draft.title_page.title.space_after}
              onChange={(v) =>
                onChangeDraft(withTitlePageBlock(draft, "title", { space_after: v }))
              }
            />
          </div>
          <AlignmentField
            label="Подписи: выравнивание"
            value={draft.title_page.signatures.alignment}
            onChange={(v) =>
              onChangeDraft(withTitlePageBlock(draft, "signatures", { alignment: v }))
            }
          />
          <div className="grid grid-cols-2 gap-3">
            <NumberField
              label="Подписи: отступ слева, мм"
              value={draft.title_page.signatures.left_indent}
              onChange={(v) =>
                onChangeDraft(
                  withTitlePageBlock(draft, "signatures", { left_indent: v }),
                )
              }
            />
            <NumberField
              label="Подвал: отступ до, pt"
              value={draft.title_page.footer.space_before}
              onChange={(v) =>
                onChangeDraft(withTitlePageBlock(draft, "footer", { space_before: v }))
              }
            />
          </div>
          <ToggleRow
            title="Подписи: сохранять табуляцию (черта у правого поля)"
            checked={draft.title_page.signatures.preserve_tabs}
            onChange={(v) =>
              onChangeDraft(withTitlePageBlock(draft, "signatures", { preserve_tabs: v }))
            }
          />
          <NumberField
            label="Подписи: позиция табуляции, мм"
            value={draft.title_page.signatures.tab_stop_mm}
            onChange={(v) =>
              onChangeDraft(withTitlePageBlock(draft, "signatures", { tab_stop_mm: v }))
            }
          />
          <AlignmentField
            label="Подвал (город/год): выравнивание"
            value={draft.title_page.footer.alignment}
            onChange={(v) =>
              onChangeDraft(withTitlePageBlock(draft, "footer", { alignment: v }))
            }
          />
          <ToggleRow
            title="Подвал прижат к нижнему полю"
            checked={draft.title_page.footer.anchor_bottom}
            onChange={(v) =>
              onChangeDraft(withTitlePageBlock(draft, "footer", { anchor_bottom: v }))
            }
          />
        </>
      )}
      
      {draft.special_sections && (
        <>
          <FieldSeparator>Специальные разделы (реферат / задание)</FieldSeparator>
          <ToggleRow
            title="Применять правила спецразделов"
            checked={draft.special_sections.enabled}
            onChange={(v) => onChangeDraft(withSpecialSections(draft, { enabled: v }))}
          />
          <AlignmentField
            label="Название раздела: выравнивание"
            value={draft.special_sections.title_alignment}
            onChange={(v) =>
              onChangeDraft(withSpecialSections(draft, { title_alignment: v }))
            }
          />
          <div className="grid grid-cols-2 gap-3">
            <NumberField
              label="Название: отступ, мм"
              value={draft.special_sections.title_first_line_indent}
              onChange={(v) =>
                onChangeDraft(withSpecialSections(draft, { title_first_line_indent: v }))
              }
            />
            <NumberField
              label="Название: кегль, pt"
              value={draft.special_sections.title_font_size}
              onChange={(v) =>
                onChangeDraft(withSpecialSections(draft, { title_font_size: v }))
              }
            />
          </div>
          <ToggleRow
            title="Название полужирным"
            checked={draft.special_sections.title_bold}
            onChange={(v) =>
              onChangeDraft(withSpecialSections(draft, { title_bold: v }))
            }
          />
          <AlignmentField
            label="Текст раздела: выравнивание"
            value={draft.special_sections.body_alignment}
            onChange={(v) =>
              onChangeDraft(withSpecialSections(draft, { body_alignment: v }))
            }
          />
          <div className="grid grid-cols-2 gap-3">
            <NumberField
              label="Текст: отступ 1-й строки, мм"
              value={draft.special_sections.body_first_line_indent}
              onChange={(v) =>
                onChangeDraft(
                  withSpecialSections(draft, { body_first_line_indent: v }),
                )
              }
            />
            <NumberField
              label="Текст: интервал, крат."
              value={draft.special_sections.body_line_spacing}
              onChange={(v) =>
                onChangeDraft(withSpecialSections(draft, { body_line_spacing: v }))
              }
            />
          </div>
          <ToggleRow
            title="Структурные разделы — с новой страницы"
            checked={draft.special_sections.page_break_before}
            onChange={(v) =>
              onChangeDraft(withSpecialSections(draft, { page_break_before: v }))
            }
          />
          <div className="grid grid-cols-2 gap-3">
            <NumberField
              label="Содержание: табуляция, мм"
              value={draft.special_sections.toc_right_tab_mm}
              onChange={(v) =>
                onChangeDraft(withSpecialSections(draft, { toc_right_tab_mm: v }))
              }
            />
            <ToggleRow
              title="Точки в содержании"
              checked={draft.special_sections.toc_rebuild_entries}
              onChange={(v) =>
                onChangeDraft(withSpecialSections(draft, { toc_rebuild_entries: v }))
              }
            />
          </div>
        </>
      )}
      
      {draft.page_numbering && (
        <>
          <FieldSeparator>Нумерация страниц</FieldSeparator>
          <ToggleRow
            title="Нумеровать страницы"
            checked={draft.page_numbering.enabled}
            onChange={(v) => onChangeDraft(withPageNumbering(draft, { enabled: v }))}
          />
          <AlignmentField
            label="Положение номера"
            value={draft.page_numbering.alignment}
            onChange={(v) => onChangeDraft(withPageNumbering(draft, { alignment: v }))}
          />
          <div className="grid grid-cols-2 gap-3">
            <NumberField
              label="Старт с номера"
              value={draft.page_numbering.start_at}
              step={1}
              onChange={(v) => onChangeDraft(withPageNumbering(draft, { start_at: v }))}
            />
            <NumberField
              label="Виден со страницы"
              value={draft.page_numbering.show_from_page}
              step={1}
              onChange={(v) =>
                onChangeDraft(withPageNumbering(draft, { show_from_page: v }))
              }
            />
          </div>
          <ToggleRow
            title="Показывать на титульном листе"
            checked={draft.page_numbering.show_on_first_page}
            onChange={(v) =>
              onChangeDraft(withPageNumbering(draft, { show_on_first_page: v }))
            }
          />
          <ToggleRow
            title="Показывать на задании"
            checked={draft.page_numbering.show_on_task_page}
            onChange={(v) =>
              onChangeDraft(withPageNumbering(draft, { show_on_task_page: v }))
            }
          />
        </>
      )}
      
      <FieldSeparator>Таблицы</FieldSeparator>
      <AlignmentField
        label="Выравнивание таблицы"
        value={draft.tables.table_alignment}
        allowInherit
        onChange={(v) =>
          onChangeDraft(withSection(draft, "tables", { table_alignment: v === "" ? null : v }))
        }
      />
      <AlignmentField
        label="Текст в ячейках"
        value={draft.tables.alignment}
        onChange={(v) => onChangeDraft(withSection(draft, "tables", { alignment: v }))}
      />
      <TextField
        label="Гарнитура ячеек"
        value={draft.tables.font_name}
        onChange={(v) => onChangeDraft(withSection(draft, "tables", { font_name: v }))}
      />
      <div className="grid grid-cols-2 gap-3">
        <NumberField
          label="Кегль, pt"
          value={draft.tables.font_size}
          onChange={(v) => onChangeDraft(withSection(draft, "tables", { font_size: v }))}
        />
        <NumberField
          label="Отступ верх, мм"
          value={draft.tables.cell_margin_top}
          onChange={(v) => onChangeDraft(withSection(draft, "tables", { cell_margin_top: v }))}
        />
        <NumberField
          label="Отступ низ, мм"
          value={draft.tables.cell_margin_bottom}
          onChange={(v) =>
            onChangeDraft(withSection(draft, "tables", { cell_margin_bottom: v }))
          }
        />
        <NumberField
          label="Отступ слева, мм"
          value={draft.tables.cell_margin_left}
          onChange={(v) => onChangeDraft(withSection(draft, "tables", { cell_margin_left: v }))}
        />
        <NumberField
          label="Отступ справа, мм"
          value={draft.tables.cell_margin_right}
          onChange={(v) =>
            onChangeDraft(withSection(draft, "tables", { cell_margin_right: v }))
          }
        />
      </div>
      
      <FieldSeparator>Подписи к рисункам и таблицам</FieldSeparator>
      <AlignmentField
        label="Выравнивание подписи"
        value={draft.captions.alignment}
        onChange={(v) => onChangeDraft(withSection(draft, "captions", { alignment: v }))}
      />
      <div className="grid grid-cols-2 gap-3">
        <NumberField
          label="Кегль, pt"
          value={draft.captions.font_size}
          onChange={(v) => onChangeDraft(withSection(draft, "captions", { font_size: v }))}
        />
        <NumberField
          label="После, pt"
          value={draft.captions.space_after}
          onChange={(v) => onChangeDraft(withSection(draft, "captions", { space_after: v }))}
        />
      </div>
      
      <FieldSeparator>Графические элементы</FieldSeparator>
      <AlignmentField
        label="Выравнивание рисунков"
        value={draft.figures.alignment}
        onChange={(v) => onChangeDraft(withSection(draft, "figures", { alignment: v }))}
      />
      
      <FieldSeparator>Списки</FieldSeparator>
      <div className="grid grid-cols-2 gap-3">
        <NumberField
          label="Отступ слева, мм"
          value={draft.lists.left_indent}
          onChange={(v) => onChangeDraft(withSection(draft, "lists", { left_indent: v }))}
        />
        <NumberField
          label="Шаг уровня, мм"
          value={draft.lists.level_indent}
          onChange={(v) => onChangeDraft(withSection(draft, "lists", { level_indent: v }))}
        />
      </div>
      <AlignmentField
        label="Выравнивание"
        value={draft.lists.alignment}
        onChange={(v) => onChangeDraft(withSection(draft, "lists", { alignment: v }))}
      />
    </>
  );
}
