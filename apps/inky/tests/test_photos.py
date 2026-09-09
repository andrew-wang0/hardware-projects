from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from photos import (
    load_displayed,
    neighbor_photo,
    photo_choices,
    photo_label,
    resolve_photo_choice,
    resolve_photo_path,
    save_displayed,
)


def write_photo(directory: Path, name: str, mtime: int | None = None) -> Path:
    import os

    path = directory / name
    path.write_bytes(b"png")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class PhotoLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.image_dir = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_labels_timestamp_files_and_keeps_newest_first(self) -> None:
        older = write_photo(self.image_dir, "1700000000.png", mtime=1)
        newer = write_photo(self.image_dir, "1800000000.png", mtime=2)
        write_photo(self.image_dir, "notes.txt", mtime=3)

        choices = photo_choices(self.image_dir, limit=10)

        self.assertEqual([choice.path for choice in choices], [newer, older])
        self.assertEqual(choices[0].label, photo_label(newer))
        self.assertEqual(
            choices[0].label,
            datetime.fromtimestamp(1_800_000_000).strftime("%Y-%m-%d %H:%M:%S"),
        )

    def test_includes_displayed_photo_beyond_limit(self) -> None:
        oldest = write_photo(self.image_dir, "1700000000.png", mtime=1)
        write_photo(self.image_dir, "1800000000.png", mtime=2)
        write_photo(self.image_dir, "1900000000.png", mtime=3)

        choices = photo_choices(self.image_dir, limit=1, include=oldest)

        self.assertEqual([choice.path.name for choice in choices], [
            "1900000000.png",
            "1700000000.png",
        ])

    def test_resolves_label_or_filename(self) -> None:
        path = write_photo(self.image_dir, "1700000000.png")
        choices = photo_choices(self.image_dir, limit=10)

        self.assertEqual(resolve_photo_choice(choices[0].label, choices, self.image_dir), path)
        self.assertEqual(resolve_photo_choice("1700000000.png", choices, self.image_dir), path)

    def test_rejects_path_traversal(self) -> None:
        write_photo(self.image_dir, "1700000000.png")
        outside = Path(self._temp.name).parent / "secret.png"
        outside.write_bytes(b"nope")
        choices = photo_choices(self.image_dir, limit=10)

        with self.assertRaisesRegex(ValueError, "invalid photo name"):
            resolve_photo_choice("../secret.png", choices, self.image_dir)
        with self.assertRaisesRegex(ValueError, "invalid photo name"):
            resolve_photo_path(self.image_dir, "../secret.png")
        with self.assertRaisesRegex(ValueError, "invalid photo name"):
            resolve_photo_path(self.image_dir, ".displayed")

    def test_persists_displayed_selection(self) -> None:
        path = write_photo(self.image_dir, "1700000000.png")
        save_displayed(self.image_dir, path)

        self.assertEqual(load_displayed(self.image_dir), path)
        self.assertFalse((self.image_dir / ".displayed").name.endswith(".png"))

    def test_load_displayed_falls_back_to_newest(self) -> None:
        write_photo(self.image_dir, "1700000000.png", mtime=1)
        newest = write_photo(self.image_dir, "1800000000.png", mtime=2)

        self.assertEqual(load_displayed(self.image_dir), newest)

    def test_disambiguates_duplicate_labels(self) -> None:
        write_photo(self.image_dir, "a.png")
        write_photo(self.image_dir, "b.png")

        with patch("photos.photo_label", return_value="same"):
            choices = photo_choices(self.image_dir, limit=10)

        labels = {choice.label for choice in choices}
        self.assertEqual(labels, {"same · a.png", "same · b.png"})

    def test_steps_to_neighbor_photos(self) -> None:
        older = write_photo(self.image_dir, "1700000000.png", mtime=1)
        newer = write_photo(self.image_dir, "1800000000.png", mtime=2)
        choices = photo_choices(self.image_dir, limit=10)

        self.assertEqual(neighbor_photo(choices, newer, 1), older)
        self.assertEqual(neighbor_photo(choices, older, -1), newer)
        self.assertEqual(neighbor_photo(choices, newer, -1), older)


if __name__ == "__main__":
    unittest.main()
