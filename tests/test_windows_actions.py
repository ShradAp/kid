"""Closed Windows actions: real local files, no arbitrary shell or network."""
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
from code_workspace import create_windows_file, windows_diagnostic


class WindowsActionTests(unittest.TestCase):
    def test_real_file_creation_and_docx_payload(self):
        with tempfile.TemporaryDirectory() as root:
            result=create_windows_file(root,"test.md","456пока")
            self.assertTrue(result["ok"])
            self.assertEqual(Path(result["path"]).read_text(encoding="utf-8"),"456пока")
            result=create_windows_file(root,"test.docx","123привет <&>")
            with zipfile.ZipFile(result["path"]) as archive:
                xml=archive.read("word/document.xml").decode("utf-8")
                self.assertIn("123привет &lt;&amp;&gt;",xml)

    def test_traversal_executable_formats_and_overwrites_are_blocked(self):
        with tempfile.TemporaryDirectory() as root:
            for name in ("../escape.md","C:\\escape.txt","test.txt:secret","NUL.txt","bad.ps1","bad.cmd","bad.lnk"):
                with self.assertRaises(ValueError):create_windows_file(root,name,"text")
            create_windows_file(root,"safe.txt","original")
            with self.assertRaises(ValueError):create_windows_file(root,"safe.txt","overwrite")
            self.assertEqual((Path(root)/"safe.txt").read_text(),"original")

    def test_general_shell_and_network_mutating_arguments_are_blocked(self):
        for command in ("ipconfig /renew","ipconfig & curl example.com","powershell.exe","cmd /c ipconfig","ipconfig /registerdns"):
            with self.assertRaises(ValueError):windows_diagnostic(command)

    @unittest.skipUnless(os.name=="nt","Native Windows diagnostic")
    def test_ipconfig_really_runs_on_windows(self):
        result=windows_diagnostic("ipconfig")
        self.assertTrue(result["ok"])
        self.assertEqual(result["platform"],"Windows")
        self.assertGreater(len(result["stdout"]),20)
