from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
import re


TIMESTAMP_NAME = re.compile(r"^[0-9]+$")
OBJECT_ID_SAFE = re.compile(r"[^a-zA-Z0-9_]+")
DISPLAYED_STATE_NAME = ".displayed"
THUMBNAIL_WIDTH = 400


@dataclass(frozen=True)
class PhotoChoice:
    label: str
    path: Path


def list_stored_photos(image_dir: Path, limit: int | None = None) -> list[Path]:
    photos = [
        path
        for path in image_dir.glob("*.png")
        if path.is_file() and not path.name.startswith(".")
    ]
    photos.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
    if limit is not None:
        photos = photos[:limit]
    return photos


def photo_label(path: Path) -> str:
    if TIMESTAMP_NAME.fullmatch(path.stem) is None:
        return path.name
    return datetime.fromtimestamp(int(path.stem)).strftime("%Y-%m-%d %H:%M:%S")


def photo_object_id(path: Path) -> str:
    stem = OBJECT_ID_SAFE.sub("_", path.stem).strip("_")
    if not stem:
        raise ValueError("invalid photo name")
    return f"photo_{stem}"


def encode_photo_thumbnail(
    path: Path,
    max_width: int = THUMBNAIL_WIDTH,
) -> tuple[bytes, str]:
    try:
        from PIL import Image
    except ImportError:
        return path.read_bytes(), "image/png"

    try:
        with Image.open(path) as image:
            rgb = image.convert("RGB")
            width, height = rgb.size
            if width > max_width > 0:
                resized_height = max(1, round(height * max_width / width))
                rgb = rgb.resize((max_width, resized_height), Image.LANCZOS)
            buffer = BytesIO()
            rgb.save(buffer, format="JPEG", quality=70)
            return buffer.getvalue(), "image/jpeg"
    except Exception:
        return path.read_bytes(), "image/png"


def photo_choices(
    image_dir: Path,
    limit: int,
    include: Path | None = None,
) -> list[PhotoChoice]:
    photos = list_stored_photos(image_dir, limit)
    names = {path.name for path in photos}
    if include is not None and include.name not in names:
        try:
            photos.append(resolve_photo_path(image_dir, include.name))
        except ValueError:
            pass

    labels = [photo_label(path) for path in photos]
    duplicates = {label for label in labels if labels.count(label) > 1}
    return [
        PhotoChoice(
            label=f"{label} · {path.name}" if label in duplicates else label,
            path=path,
        )
        for label, path in zip(labels, photos)
    ]


def resolve_photo_path(image_dir: Path, name: str) -> Path:
    if not name or name.startswith(".") or name != Path(name).name:
        raise ValueError("invalid photo name")
    if Path(name).suffix.lower() != ".png":
        raise ValueError("photo must be a PNG file")

    image_dir = image_dir.resolve()
    path = (image_dir / name).resolve()
    if path.parent != image_dir or not path.is_file():
        raise ValueError(f"unknown photo: {name}")
    return path


def resolve_photo_choice(
    option: str,
    choices: list[PhotoChoice],
    image_dir: Path,
) -> Path:
    option = option.strip()
    if not option:
        raise ValueError("photo selection cannot be empty")
    for choice in choices:
        if option in {choice.label, choice.path.name}:
            return choice.path
    return resolve_photo_path(image_dir, option)


def load_displayed(image_dir: Path) -> Path | None:
    try:
        name = (image_dir / DISPLAYED_STATE_NAME).read_text(encoding="utf-8").strip()
    except OSError:
        name = ""
    if name:
        try:
            return resolve_photo_path(image_dir, name)
        except ValueError:
            pass
    photos = list_stored_photos(image_dir, limit=1)
    return photos[0] if photos else None


def save_displayed(image_dir: Path, path: Path) -> None:
    (image_dir / DISPLAYED_STATE_NAME).write_text(f"{path.name}\n", encoding="utf-8")
