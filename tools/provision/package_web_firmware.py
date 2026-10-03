"""Package the merged lectio_s3 image for the browser flasher."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_BUILD_IMAGE = _ROOT / "firmware" / ".pio" / "build" / "lectio_s3" / "merged_firmware.bin"
_FIRMWARE_DIR = (
    _ROOT
    / "services"
    / "lectio-gateway"
    / "src"
    / "lectio_gateway"
    / "static"
    / "firmware"
)
_FIRMWARE_NAME = "lectio_s3_merged.bin"


def _source_revision() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "local-build"
    return result.stdout.strip() or "local-build"


def package_firmware() -> dict[str, object]:
    if not _BUILD_IMAGE.is_file():
        raise FileNotFoundError(
            f"{_BUILD_IMAGE} is missing; run `pio run -d firmware -e lectio_s3` first."
        )

    image = _BUILD_IMAGE.read_bytes()
    if not image:
        raise ValueError("The merged lectio_s3 firmware image is empty.")

    _FIRMWARE_DIR.mkdir(parents=True, exist_ok=True)
    destination = _FIRMWARE_DIR / _FIRMWARE_NAME
    with tempfile.NamedTemporaryFile(dir=_FIRMWARE_DIR, delete=False) as temporary:
        temporary.write(image)
        temporary_path = Path(temporary.name)
    temporary_path.replace(destination)

    manifest: dict[str, object] = {
        "target": "lectio_s3",
        "chip": "ESP32-S3",
        "version": _source_revision(),
        "file": _FIRMWARE_NAME,
        "flash_address": "0x0",
        "flash_mode": "dio",
        "flash_frequency": "40m",
        "flash_size": "4MB",
        "size_bytes": len(image),
        "sha256": hashlib.sha256(image).hexdigest(),
    }
    manifest_path = _FIRMWARE_DIR / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    try:
        manifest = package_firmware()
    except (OSError, ValueError) as error:
        print(f"Could not package browser firmware: {error}")
        return 1
    print(
        "Packaged {size_bytes} bytes for {target} ({sha256})".format(**manifest)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
