"""Run the real PowerShell loader against a local release fixture, offline."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell loader')
class LoaderIntegrationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='kid-loader-test-')
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.managed = self.folder / 'kid-launcher'
        self.make_release()

    def make_release(self, fail=False, traversal=False):
        self.archive = self.folder / 'release.zip'
        with zipfile.ZipFile(self.archive, 'w') as archive:
            for name in ('installer/runtime.json', 'desktop_app.py', 'requirements.txt', 'UI/package-lock.json'):
                archive.writestr(name, '{}')
            archive.write(ROOT / 'installer/loader.ps1', 'installer/loader.ps1')
            archive.writestr('install.ps1', 'param([switch]$ManagedRuntime)\n' + ('exit 23' if fail else
                '@{schema=1; complete=$true; node="fixture"} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PSScriptRoot ".install-state.json") -Encoding UTF8'))
            if traversal:
                archive.writestr('../escaped.txt', 'bad')
        self.sums = self.folder / 'sums'
        self.sums.write_text(hashlib.sha256(self.archive.read_bytes()).hexdigest() + '  kid-source.zip\n')

    def run_loader(self, tag='v1.0.0'):
        def quoted(value):
            return "'" + str(value).replace("'", "''") + "'"
        # Mock only network/UI; execute extraction, installation subprocess,
        # pointer commit, actual file locking and loader replacement unchanged.
        script = f'''
function Invoke-RestMethod {{
    return [pscustomobject]@{{draft=$false; prerelease=$false; tag_name={quoted(tag)}; assets=@(
        [pscustomobject]@{{name='kid-source.zip'; browser_download_url='https://fixture/source'}},
        [pscustomobject]@{{name='SHA256SUMS'; browser_download_url='https://fixture/sums'}}
    )}}
}}
function Invoke-WebRequest {{
    param($Uri, $OutFile, [switch]$UseBasicParsing, $TimeoutSec, $Headers)
    $source = if ($Uri -eq 'https://fixture/source') {{ {quoted(self.archive)} }} else {{ {quoted(self.sums)} }}
    Copy-Item -LiteralPath $source -Destination $OutFile -Force
}}
function Read-Host {{ param($Prompt); return '' }}
function New-Object {{
    if ($args -notcontains 'WScript.Shell') {{ return Microsoft.PowerShell.Utility\\New-Object @args }}
    $object = [pscustomobject]@{{}}
    $object | Add-Member ScriptMethod CreateShortcut {{
        param($Path)
        $shortcut = [pscustomobject]@{{TargetPath=''; Arguments=''; WorkingDirectory=''}}
        $shortcut | Add-Member ScriptMethod Save {{}}
        return $shortcut
    }}
    return $object
}}
& {quoted(ROOT / 'installer/loader.ps1')} -NoLaunch
'''
        encoded = base64.b64encode(script.encode('utf-16-le')).decode()
        return subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encoded],
                              env={**os.environ, 'LOCALAPPDATA': str(self.folder)}, capture_output=True,
                              text=True, errors='replace', timeout=40)

    def test_success_then_failed_update_preserves_previous_pointer(self):
        first = self.run_loader()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        pointer = self.managed / 'current.json'
        current = json.loads(pointer.read_text(encoding='utf-8-sig'))
        self.assertEqual(current['tag'], 'v1.0.0')
        self.assertTrue((self.managed / 'loader.ps1').is_file())
        self.make_release(fail=True)
        failed = self.run_loader('v1.0.1')
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(json.loads(pointer.read_text(encoding='utf-8-sig')), current)
        self.make_release()
        retried = self.run_loader('v1.0.1')
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        next_pointer = json.loads(pointer.read_text(encoding='utf-8-sig'))
        self.assertEqual(next_pointer['tag'], 'v1.0.1')
        self.assertNotEqual(next_pointer['directory'], current['directory'])
        self.assertEqual(json.loads((self.managed / 'current.json.previous').read_text(encoding='utf-8-sig')), current)

    def test_checksum_failure_never_activates(self):
        self.sums.write_text('0' * 64 + '  kid-source.zip\n')
        result = self.run_loader()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.managed / 'current.json').exists())

    def test_zip_traversal_never_extracted_or_activated(self):
        self.make_release(traversal=True)
        result = self.run_loader()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.managed / 'current.json').exists())
        self.assertFalse((self.managed / 'versions/escaped.txt').exists())


if __name__ == '__main__':
    unittest.main()
