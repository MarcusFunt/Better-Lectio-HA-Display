import hashlib
import os
import tempfile
import time
from pathlib import Path

VERSION_RETENTION_SECONDS = 7 * 24 * 60 * 60


def image_filename(content):
    return f"{hashlib.sha256(content).hexdigest()[:16]}.bmp"


def version_directory(image_path):
    image_path = Path(image_path)
    return image_path.parent / f"{image_path.stem}.versions"


def version_path(image_path, filename):
    return version_directory(image_path) / filename


def preserve_image_version(content, image_path):
    image_path = Path(image_path)
    directory = version_directory(image_path)
    directory.mkdir(parents=True, exist_ok=True)
    filename = image_filename(content)
    target = directory / filename
    if target.exists():
        target.touch()
    else:
        descriptor, temporary_name = tempfile.mkstemp(dir=directory, prefix=f".{filename}.", suffix=".tmp")
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, target)
        finally:
            temporary_path.unlink(missing_ok=True)
    cutoff = time.time() - VERSION_RETENTION_SECONDS
    for old_version in directory.glob("*.bmp"):
        if old_version != target and old_version.stat().st_mtime < cutoff:
            old_version.unlink(missing_ok=True)
    return filename


def snapshot_image(image_path):
    image_path = Path(image_path)
    return preserve_image_version(image_path.read_bytes(), image_path)
