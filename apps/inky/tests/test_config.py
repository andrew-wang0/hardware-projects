from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config


class PhotoSelectLimitConfigTests(unittest.TestCase):
    def test_rejects_non_positive_limit(self) -> None:
        os.environ["INKY_PHOTO_SELECT_LIMIT"] = "0"
        self.addCleanup(os.environ.pop, "INKY_PHOTO_SELECT_LIMIT", None)

        with self.assertRaisesRegex(ValueError, "INKY_PHOTO_SELECT_LIMIT"):
            load_config()


if __name__ == "__main__":
    unittest.main()
