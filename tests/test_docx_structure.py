from pathlib import Path
import tempfile
import unittest

from isolated_case import IsolatedStorageTestCase


class DocxStructureTests(IsolatedStorageTestCase):
    def test_section_recognition_and_explicit_vs_rendered_page_breaks(self):
        from docx import Document
        from docx.oxml import OxmlElement
        from services.word_automation.docx_engine import (
            _classify_document_sections, _has_explicit_page_break, _paragraph_breaks_page,
        )
        from services.word_automation.preset_schema import default_gost_preset

        preset = default_gost_preset()
        preset.title_page.enabled = False
        document = Document()
        for text in ("РЕФЕРАТ", "Краткое описание", "ВВЕДЕНИЕ", "Основной текст", "1.1 Подраздел"):
            document.add_paragraph(text)
        sections = _classify_document_sections(document, preset)
        self.assertEqual([p.text for p in sections.special["referat"]], ["РЕФЕРАТ", "Краткое описание"])
        self.assertEqual([p.text for p in sections.structural], ["ВВЕДЕНИЕ"])
        self.assertEqual([(level, p.text) for level, p in sections.subheadings], [(2, "1.1 Подраздел")])
        paragraph = document.add_paragraph("rendered")
        paragraph.add_run()._r.append(OxmlElement("w:lastRenderedPageBreak"))
        self.assertTrue(_paragraph_breaks_page(paragraph))
        self.assertFalse(_has_explicit_page_break(paragraph))
        paragraph.paragraph_format.page_break_before = False
        self.assertFalse(_has_explicit_page_break(paragraph))
        paragraph.paragraph_format.page_break_before = True
        self.assertTrue(_has_explicit_page_break(paragraph))

    def test_content_aliases_lists_ragged_tables_and_inline_styles(self):
        from services.word_automation import normalize_structure, parse_inline_runs

        blocks = normalize_structure([
            {"type": "h2", "text": "Heading"},
            {"type": "paragraph", "text": "Body"},
            {"type": "ol", "items": ["one", "two"]},
            {"type": "table", "rows": [["a", "b"], ["c"]]},
        ])
        self.assertEqual([b.kind for b in blocks], ["heading", "paragraph", "list", "table"])
        self.assertEqual(blocks[0].level, 2)
        self.assertTrue(blocks[2].ordered)
        self.assertEqual(blocks[2].items, ("one", "two"))
        self.assertEqual(blocks[3].rows, (("a", "b"), ("c", "")))
        runs = parse_inline_runs("plain **bold** and *italic*")
        self.assertEqual("".join(run.text for run in runs), "plain bold and italic")
        self.assertTrue(any(run.bold and run.text == "bold" for run in runs))
        self.assertTrue(any(run.italic and run.text == "italic" for run in runs))

    def test_formatter_rules_and_extractor_preserve_content_and_ooxml_properties(self):
        from docx import Document
        from services.word_automation import DocxFormatter, apply_rules, preview_document
        from services.word_automation.docx_engine import set_run_font, apply_paragraph_format
        from services.word_automation.preset_schema import Alignment, LineSpacingRule

        with tempfile.TemporaryDirectory(prefix="kid-docx-structure-") as directory:
            formatter = DocxFormatter(output_dir=directory)
            path = formatter.create_document([
                {"type": "h1", "text": "Introduction"},
                {"type": "paragraph", "text": "Text **bold**"},
                {"type": "table", "header": ["left", "right"], "rows": [["one", "two"]]},
            ])
            document = Document(path)
            paragraph = next(p for p in document.paragraphs if "Text" in p.text)
            apply_paragraph_format(paragraph.paragraph_format, alignment=Alignment.RIGHT,
                                   first_line_indent_mm=5, space_before=7,
                                   space_after=8, line_spacing=1.5,
                                   line_spacing_rule=LineSpacingRule.MULTIPLE)
            set_run_font(paragraph.runs[0], font_name="Arial", size_pt=11, bold=True)
            document.save(path)
            changed = apply_rules(path, [{"scope": "page", "property": "left", "value": 26}], output_path=path)
            self.assertEqual(changed["path"], path)
            current = Document(path)
            self.assertAlmostEqual(current.sections[0].left_margin.mm, 26, delta=0.1)
            self.assertEqual(current.tables[0].cell(1, 1).text, "two")
            self.assertTrue(preview_document(path)["blocks"])
            body = next(p for p in current.paragraphs if "Text" in p.text)
            self.assertEqual(body.runs[0].font.name, "Arial")
            self.assertAlmostEqual(body.paragraph_format.first_line_indent.mm, 5, delta=0.1)
