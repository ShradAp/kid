"""Storage regressions run in fresh processes with disposable user data."""
import os
import subprocess
import sys
import tempfile
import unittest


class StorageStabilityTests(unittest.TestCase):
    def run_isolated(self, code):
        with tempfile.TemporaryDirectory(prefix="kid-storage-") as directory:
            result = subprocess.run([sys.executable, "-c", code], env=dict(os.environ, KID_CONFIG_DIR=directory),
                                    capture_output=True, encoding="utf-8", errors="replace", timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_concurrent_main_chat_writes_remain_valid(self):
        self.run_isolated('''
from concurrent.futures import ThreadPoolExecutor
import config_manager as cm
cm.load_config()
def save(number):
 cm.save_chat_history('concurrent', [{'role':'user','content':str(number)*1000}])
with ThreadPoolExecutor(max_workers=8) as pool:
 list(pool.map(save, range(40)))
history=cm.load_chat_history('concurrent')
assert len(history)==1 and history[0]['role']=='user'
assert not list(cm.CHAT_HISTORY_DIR.glob('*.tmp'))
''')

    def test_failed_main_chat_replace_preserves_previous_history(self):
        self.run_isolated('''
from unittest.mock import patch
import config_manager as cm
cm.load_config()
cm.save_chat_history('one',[{'role':'user','content':'preserved'}])
with patch('config_manager.os.replace',side_effect=OSError('write failed')):
 try: cm.save_chat_history('one',[{'role':'user','content':'new'}])
 except OSError: pass
 else: raise AssertionError('Write failure must propagate')
assert cm.load_chat_history('one')[0]['content']=='preserved'
assert not list(cm.CHAT_HISTORY_DIR.glob('*.tmp'))
''')

    def test_already_migrated_history_is_not_read_again(self):
        self.run_isolated('''
from unittest.mock import patch
from pathlib import Path
import config_manager as cm
cm.load_config()
cm.LEGACY_HISTORY_DIR=cm.CONFIG_DIR/'old'
cm.LEGACY_HISTORY_DIR.mkdir()
cm.LEGACY_HISTORY_PATH=cm.CONFIG_DIR/'absent-history.json'
cm.LEGACY_SESSIONS_META_PATH=cm.CONFIG_DIR/'absent-meta.json'
old=cm.LEGACY_HISTORY_DIR/'history_one.json'
old.write_text('[{"role":"user","content":"old"}]',encoding='utf-8')
cm.save_chat_history('legacy_one',[{'role':'user','content':'current'}])
with patch.object(Path,'read_text',side_effect=AssertionError('Already migrated source must not be read')):
 cm._migrate_chat_storage({'sessions_meta':{}})
assert cm.load_chat_history('legacy_one')[0]['content']=='current'
''')

    def test_concurrent_note_prepend_does_not_lose_notes(self):
        self.run_isolated('''
from concurrent.futures import ThreadPoolExecutor
import desktop_app as app
app.save_notes([])
with ThreadPoolExecutor(max_workers=8) as pool:
 list(pool.map(lambda number: app.prepend_note({'id':str(number),'content':'kept'}),range(40)))
notes=app.load_notes()
assert len(notes)==40
assert len({note['id'] for note in notes})==40
''')

    def test_note_write_failure_is_not_reported_as_saved(self):
        self.run_isolated('''
from unittest.mock import patch
import desktop_app as app
app.save_notes([{'id':'preserved'}])
with patch('desktop_app.os.replace',side_effect=OSError('write failed')):
 try: app.save_notes([{'id':'new'}])
 except OSError: pass
 else: raise AssertionError('Write failure must propagate')
assert app.load_notes()==[{'id':'preserved'}]
''')

    def test_session_clear_delete_and_late_upsert_keep_storage_and_codex_reader_consistent(self):
        self.run_isolated('''
import io,json
from unittest.mock import Mock
import desktop_app as app
class Request:
 def __init__(self,path,data):
  self.path=path
  body=json.dumps(data).encode()
  self.headers={'Content-Length':str(len(body))}
  self.rfile=io.BytesIO(body);self.wfile=io.BytesIO();self.status=None
 def _set_cors(self,status=200):self.status=status
def action(path,data):
 request=Request(path,data)
 app.BridgeHandler._do_POST(request)
 assert request.status==200,request.wfile.getvalue()
 return json.loads(request.wfile.getvalue())
app.CODEX_SERVICE.forget=Mock()
action('/api/session/upsert',{'id':'fixture','personaId':'main','title':'Kept'})
app.sessions_memory['fixture']=[{'role':'system','content':'rules'},{'role':'user','content':'question'}]
app.save_history(app.sessions_memory)
assert app.CODEX_SERVICE.history_reader('fixture')==app.sessions_memory['fixture']
action('/api/session/activate',{'id':'fixture'})
assert app.load_config()['last_active_session']=='fixture'
action('/api/session/clear',{'id':'fixture'})
assert app.load_chat_history('fixture')==[{'role':'system','content':'rules'}]
action('/api/session/delete',{'id':'fixture'})
assert app.load_config()['last_active_session']==''
assert 'fixture' not in app.sessions_memory and 'fixture' not in app.load_sessions_meta()
assert app.CODEX_SERVICE.history_reader('fixture')==[]
assert action('/api/session/upsert',{'id':'fixture'})['deleted']
assert action('/api/session/activate',{'id':'fixture'})['deleted']
action('/api/session/clear',{'id':'fixture'})
assert not (app.USER_DATA_DIR/'history'/'fixture.json').exists()
assert app.CODEX_SERVICE.forget.call_count==3
''')


if __name__ == "__main__":
    unittest.main()
