"""Offline release/update and interrupted-download regressions."""
import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from services import updates
from scripts.prepare_native_codex import atomic_write, verified_archive
from native_codex.wfp import ProtectionUnavailable


class UpdateTests(unittest.TestCase):
    def response(self, tag='v1.2.0', assets=('kid-source.zip', 'SHA256SUMS')):
        return Mock(status_code=200, json=lambda: {'tag_name': tag, 'assets': [{'name': name} for name in assets]})

    def check(self, tag, current='v1.1.0', assets=('kid-source.zip', 'SHA256SUMS')):
        with patch.object(updates.requests, 'get', return_value=self.response(tag, assets)), \
                patch.object(updates, 'installed_tag', return_value=current), \
                patch.object(updates, 'managed_install', return_value=True):
            return updates.check()

    def test_versions_compared_numerically_and_downgrades_not_offered(self):
        self.assertTrue(self.check('v1.10.0', 'v1.9.0')['available'])
        self.assertFalse(self.check('v1.9.0', 'v1.10.0')['available'])
        self.assertFalse(self.check('v1.1.0')['available'])

    def test_incomplete_release_is_not_offered(self):
        self.assertFalse(self.check('v1.2.0', assets=('kid-source.zip',))['available'])

    def test_no_releases_is_normal(self):
        with patch.object(updates.requests, 'get', return_value=Mock(status_code=404)):
            self.assertFalse(updates.check()['available'])

    def test_untrusted_tag_is_rejected(self):
        for tag in ('main', '../v1.0.0', 'v1.0.0;calc', '-File evil.ps1', 'v1.0.0-beta'):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                updates.install(tag)

    def test_source_checkout_is_never_modified(self):
        with patch.object(updates, 'managed_install', return_value=False), \
                patch.object(updates.subprocess, 'Popen') as start:
            with self.assertRaises(ValueError):
                updates.install('v1.2.0')
            start.assert_not_called()

    def test_confirm_required_before_start(self):
        handler = Mock(headers={'Content-Length': '2'}, rfile=io.BytesIO(b'{}'), wfile=io.BytesIO())
        with patch.object(updates, 'install') as start:
            updates.handle(handler, 'POST')
            start.assert_not_called()
        handler._set_cors.assert_called_once_with(400)

    def test_confirmed_install_starts_pinned_loader_in_separate_console(self):
        with patch.object(updates, 'managed_install', return_value=True), \
                patch.object(updates, '_installer', None), \
                patch.object(updates.subprocess, 'Popen') as start:
            self.assertTrue(updates.install('v1.2.0')['started'])
            arguments = start.call_args.args[0]
            self.assertEqual(arguments[-3:], ['-ReleaseTag', 'v1.2.0', '-NoLaunch'])
            self.assertIn(str(updates.loader_root() / 'loader.ps1'), arguments)

    def test_duplicate_installer_not_started(self):
        with patch.object(updates, 'managed_install', return_value=True), \
                patch.object(updates, '_installer', Mock(poll=lambda: None)), \
                patch.object(updates.subprocess, 'Popen') as start:
            with self.assertRaises(ValueError):
                updates.install('v1.2.0')
            start.assert_not_called()

    def test_release_metadata_read_with_powershell_bom(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(updates, 'APP_ROOT', Path(folder)):
            (Path(folder) / '.release.json').write_text(json.dumps({'schema': 1, 'tag': 'v1.2.0', 'repository': updates.REPOSITORY}), encoding='utf-8-sig')
            self.assertEqual(updates.installed_tag(), 'v1.2.0')


class DownloadRecoveryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cache = Path(folder.name) / 'package.tgz'
        self.content = b'complete verified package'
        self.integrity = 'sha512-' + base64.b64encode(hashlib.sha512(self.content).digest()).decode()

    def test_corrupt_cache_redownloaded_and_next_attempt_reuses_verified_cache(self):
        self.cache.write_bytes(b'interrupted download')
        download = Mock(return_value=self.content)
        self.assertEqual(verified_archive(self.cache, self.integrity, download), self.content)
        self.assertEqual(verified_archive(self.cache, self.integrity, download), self.content)
        download.assert_called_once()
        self.assertEqual(self.cache.read_bytes(), self.content)

    def test_invalid_download_does_not_replace_existing_cache(self):
        self.cache.write_bytes(b'old')
        with self.assertRaises(ProtectionUnavailable):
            verified_archive(self.cache, self.integrity, lambda: b'bad')
        self.assertEqual(self.cache.read_bytes(), b'old')

    def test_interrupted_atomic_replacement_preserves_old_file_and_cleans_temp(self):
        self.cache.write_bytes(b'old')
        with patch.object(Path, 'replace', side_effect=PermissionError), self.assertRaises(PermissionError):
            atomic_write(self.cache, b'new')
        self.assertEqual(self.cache.read_bytes(), b'old')
        self.assertEqual(list(self.cache.parent.iterdir()), [self.cache])


if __name__ == '__main__':
    unittest.main()
