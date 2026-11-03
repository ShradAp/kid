"""Document workflows exercised with actual DOCX files in isolated processes."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class DocumentStudioTests(unittest.TestCase):
    def test_import_keeps_word_dependencies_optional(self):
        code = """
import sys
sys.path.insert(0, sys.argv[1])
from services.document_studio import DocumentStudio
DocumentStudio(None, None, None)
assert 'docx' not in sys.modules
assert 'pydantic' not in sys.modules
"""
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", code, str(Path(__file__).resolve().parents[1])],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def run_isolated(self, code):
        with tempfile.TemporaryDirectory(prefix="kid-studio-test-") as directory:
            env = dict(os.environ, KID_CONFIG_DIR=directory,
                       KID_WORD_PRESETS_DIR=str(Path(directory) / "presets"),
                       HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
            result = subprocess.run([sys.executable, "-B", "-c", code],
                                    env=env, capture_output=True, encoding="utf-8",
                                    errors="replace", timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_analyze_manual_and_model_edits_share_document_then_close_keeps_file(self):
        self.run_isolated('''
from pathlib import Path
import json
from docx import Document
import desktop_app as app
source = app.USER_DATA_DIR / 'fixture.docx'
document = Document()
document.add_heading('Fixture', level=1)
document.add_paragraph('A paragraph that must survive all formatting.')
table = document.add_table(rows=1, cols=2)
table.cell(0,0).text = 'left'; table.cell(0,1).text = 'right'
document.save(source)
app.FILE_ACCESS.grant(str(source))
before = source.read_bytes()
analysis = app._word_analyze_payload({'source_file':str(source),'session_id':'document-studio'})
assert analysis['ok'] and not analysis['preset_saved']
assert source.read_bytes() == before
assert not list((app.USER_DATA_DIR/'presets').glob('*.json'))
state = app._word_state_payload({'session_id':'document-studio'})
assert state['has_document'] and state['path']==str(source) and state['preset_json']
app._studio_set_presets('document-studio', [analysis['preset']])
assert json.dumps(analysis['preset'], ensure_ascii=False) in app._run_word_tool('get_document_preset',{},'document-studio')
app._run_word_tool('set_page_margins', {'left':27}, 'document-studio')
assert abs(Document(source).sections[0].left_margin.mm-27)<0.1
outcome=app._word_apply_payload({'session_id':'document-studio',
 'rules':[{'scope':'page','property':'right','value':19}]})
assert outcome['ok'] and outcome['path']==str(source) and outcome['preview']
current=Document(source)
assert abs(current.sections[0].left_margin.mm-27)<0.1
assert abs(current.sections[0].right_margin.mm-19)<0.1
assert current.tables[0].cell(0,1).text=='right'
assert any('must survive' in p.text for p in current.paragraphs)
app._word_preset_save_payload({'session_id':'document-studio','preset':analysis['preset'],'preset_name':'saved-fixture'})
assert list((app.USER_DATA_DIR/'presets').glob('*.json'))
assert app._word_close_payload({'session_id':'document-studio'})['ok']
assert not app._word_state_payload({'session_id':'document-studio'})['has_document']
assert source.is_file()
''')

    def test_compile_and_unselected_document_rejection(self):
        self.run_isolated('''
from pathlib import Path
from docx import Document
import desktop_app as app
result=app._word_compile_payload({'blocks':[{'type':'paragraph','text':'Created fixture'}]})
assert result['ok'] and result['download'].startswith('/api/download-local?')
path=Path(result['path'])
assert path.is_relative_to(app.USER_DATA_DIR/'generated_files')
assert any('Created fixture' in p.text for p in Document(path).paragraphs)
source=app.USER_DATA_DIR/'not-selected.docx'
Document().save(source)
try: app._word_analyze_payload({'source_file':str(source)})
except FileNotFoundError: pass
else: raise AssertionError('Unselected document must not be analyzed')
''')

    def test_studio_profiles_and_history_remain_separate_from_main_chat(self):
        self.run_isolated('''
import desktop_app as app
cfg=app.load_config()
cfg['profiles']={'main':{'engine':'codex','api_key':'fixture-key','codex':{'proxy_password':'fixture-only'}}}
cfg['active_profile']='main'
app.save_config(cfg)
app.save_chat_history('main-chat',[{'role':'user','content':'main survives'}])
initial=app._studio_init_payload()
pid=initial['active_profile']
profile=app.load_config()['profiles'][pid]
assert profile['engine']=='api' and 'codex' not in profile
assert initial['profiles'][0]['apiKey']==''
app._studio_handle('/api/studio/session/upsert',{'id':'studio-chat','personaId':pid,'title':'Fixture'})
messages=[{'role':'system','content':'system'},{'role':'user','content':'studio only'}]
app.studio_sessions_memory['studio-chat']=messages
app.save_studio_history('studio-chat',messages)
assert app._studio_handle('/api/studio/session/messages',{'id':'studio-chat'})['messages']==messages
assert {s['id'] for s in app._studio_init_payload()['sessions']}=={'studio-chat'}
app._studio_handle('/api/studio/session/clear',{'id':'studio-chat'})
assert app.load_studio_history('studio-chat')==messages[:1]
app._studio_handle('/api/studio/session/delete',{'id':'studio-chat'})
assert not app._studio_init_payload()['sessions']
assert app.load_chat_history('main-chat')==[{'role':'user','content':'main survives'}]
try: app._studio_handle('/api/studio/save_profile',{'persona':{'id':'main'}})
except ValueError: pass
else: raise AssertionError('Studio must not replace a main profile')
''')
