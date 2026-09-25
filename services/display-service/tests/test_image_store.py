from datetime import datetime, timezone

import pytest
from display_service.image_store import DisplayImageStore, validate_display_bmp
from display_service.model import DisplayModel
from display_service.renderer import render_display


def test_bmp_validator_rejects_palette_the_firmware_cannot_display():
    model = DisplayModel(
        generated_at=datetime(2026, 9, 25, 6, 0, tzinfo=timezone.utc),
        days=(),
        sidebar=(),
    )
    bmp = bytearray(render_display(model).bmp)
    bmp[54:62] = b"\x00\x00\xff\x00\x00\xff\x00\x00"

    with pytest.raises(ValueError, match="monochrome palette"):
        validate_display_bmp(bytes(bmp))


@pytest.mark.parametrize(
    "palette",
    (
        b"\x00\x00\x00\x00\xff\xff\xff\x00",
        b"\xff\xff\xff\x00\x00\x00\x00\x00",
    ),
)
def test_bmp_validator_accepts_both_firmware_monochrome_palette_orders(palette):
    model = DisplayModel(
        generated_at=datetime(2026, 9, 25, 6, 0, tzinfo=timezone.utc),
        days=(),
        sidebar=(),
    )
    bmp = bytearray(render_display(model).bmp)
    bmp[54:62] = palette

    validate_display_bmp(bytes(bmp))


def test_read_only_image_store_cannot_publish(tmp_path):
    model = DisplayModel(
        generated_at=datetime(2026, 9, 25, 6, 0, tzinfo=timezone.utc),
        days=(),
        sidebar=(),
    )
    store = DisplayImageStore(tmp_path, read_only=True)

    with pytest.raises(PermissionError, match="read-only"):
        store.publish(render_display(model).bmp, model.generated_at)
