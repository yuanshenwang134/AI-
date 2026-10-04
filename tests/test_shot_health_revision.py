import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from aihoop import api

class CodeRevision(unittest.TestCase):
    def test_nested_python_changes_health_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);main=root/'api.py';main.touch()
            nested=root/'legacy_shots';nested.mkdir();scorer=nested/'scorer.py';scorer.touch()
            ignored=nested/'trace.json';ignored.touch()
            os.utime(main,(100,100));os.utime(scorer,(200,200));os.utime(ignored,(300,300))
            with patch.object(api,'__file__',str(main)),patch.object(api,'_LOADED_AT',150):
                self.assertEqual(api._code_rev()['newest'],200)
                self.assertTrue(asyncio.run(api.health())['stale'])
            with patch.object(api,'__file__',str(main)),patch.object(api,'_LOADED_AT',201):
                self.assertFalse(asyncio.run(api.health())['stale'])

if __name__=='__main__':unittest.main()
