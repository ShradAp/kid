"""Fresh processes for tests of modules whose user paths bind at import time."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class IsolatedStorageTestCase(unittest.TestCase):
    def run(self, result=None):
        if os.environ.get("KID_ISOLATED_STORAGE_TEST") == "1":
            return super().run(result)
        if result is None:
            result = self.defaultTestResult()
        result.startTest(self)
        try:
            root = Path(__file__).resolve().parents[1]
            with tempfile.TemporaryDirectory(prefix="kid-test-process-") as directory:
                env = dict(os.environ, KID_ISOLATED_STORAGE_TEST="1", KID_CONFIG_DIR=directory,
                           HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                           PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
                code = """
import sys, unittest
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
unittest.main(module=None, argv=['unittest', sys.argv[3]], verbosity=2)
"""
                child = subprocess.run(
                    [sys.executable, "-B", "-c", code, str(root), str(root / "tests"), self.id()],
                    cwd=root, env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=30,
                )
            if child.returncode:
                raise AssertionError(child.stdout + child.stderr)
        except AssertionError:
            result.addFailure(self, sys.exc_info())
        except BaseException:
            result.addError(self, sys.exc_info())
        else:
            result.addSuccess(self)
        finally:
            result.stopTest(self)
        return result
