import hmac
import os
import re
import secrets
import sqlite3
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file
from PIL import Image

from .images import snapshot_image, version_path


def load_settings(overrides=None):
    values = {
        "DATABASE_PATH": os.environ.get("DATABASE_PATH", "data/devices.sqlite3"),
        "IMAGE_PATH": os.environ.get("IMAGE_PATH", "data/current.bmp"),
        "CACHE_PATH": os.environ.get("CACHE_PATH", "data/events.json"),
        "PUBLIC_BASE_URL": os.environ.get("PUBLIC_BASE_URL", ""),
        "REFRESH_RATE": int(os.environ.get("REFRESH_RATE", "1800")),
        "TIMEZONE": os.environ.get("TIMEZONE", "Europe/Copenhagen"),
        "LECTIO_CACHE_TTL_SECONDS": int(os.environ.get("LECTIO_CACHE_TTL_SECONDS", "3600")),
        "CALENDAR_CACHE_TTL_SECONDS": int(os.environ.get("CALENDAR_CACHE_TTL_SECONDS", "900")),
        "LECTIO_USERNAME": os.environ.get("LECTIO_USERNAME", ""),
        "LECTIO_PASSWORD": os.environ.get("LECTIO_PASSWORD", ""),
        "LECTIO_SCHOOL_ID": os.environ.get("LECTIO_SCHOOL_ID", ""),
        "CALENDAR_ICS_URL": os.environ.get("CALENDAR_ICS_URL", ""),
    }
    values.update(overrides or {})
    values["REFRESH_RATE"] = int(values["REFRESH_RATE"])
    if not 60 <= values["REFRESH_RATE"] <= 21600:
        raise ValueError("REFRESH_RATE must be between 60 and 21600 seconds")
    for name in ("LECTIO_CACHE_TTL_SECONDS", "CALENDAR_CACHE_TTL_SECONDS"):
        values[name] = int(values[name])
        if values[name] < 60:
            raise ValueError(f"{name} must be at least 60 seconds")
    for name in ("DATABASE_PATH", "IMAGE_PATH", "CACHE_PATH"):
        values[name] = Path(values[name])
    values["PUBLIC_BASE_URL"] = str(values["PUBLIC_BASE_URL"]).rstrip("/")
    return values


def _connect(path):
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _initialize_database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS devices (
            mac TEXT PRIMARY KEY,
            api_key TEXT NOT NULL,
            friendly_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT
        )""")


def _valid_device_id(value):
    return bool(value and re.fullmatch(r"(?:[0-9A-Fa-f]{12}|[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})", value.strip()))


def _ensure_initial_image(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        Image.new("1", (800, 480), 1).save(path, format="BMP")
    snapshot_image(path)


def create_app(config=None):
    settings = load_settings(config)
    _initialize_database(settings["DATABASE_PATH"])
    _ensure_initial_image(settings["IMAGE_PATH"])
    app = Flask(__name__)
    app.config.update(TESTING=False)
    if config:
        app.config.update(config)

    def image_url(filename):
        base = settings["PUBLIC_BASE_URL"] or request.host_url.rstrip("/")
        return f"{base}/display/{filename}"

    @app.get("/api/setup")
    @app.get("/api/setup/")
    def setup_device():
        device_id = request.headers.get("ID", "")
        if not _valid_device_id(device_id):
            return jsonify(error="A valid device MAC address is required in the ID header."), 400
        mac = device_id.strip().upper()
        key = secrets.token_urlsafe(32)
        friendly_id = "TRMNL-" + re.sub(r"[^0-9A-F]", "", mac)[-6:]
        with _connect(settings["DATABASE_PATH"]) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO devices (mac, api_key, friendly_id) VALUES (?, ?, ?)",
                (mac, key, friendly_id),
            )
            row = connection.execute("SELECT * FROM devices WHERE mac = ?", (mac,)).fetchone()
        filename = snapshot_image(settings["IMAGE_PATH"])
        return jsonify(
            status=200,
            api_key=row["api_key"],
            friendly_id=row["friendly_id"],
            image_url=image_url(filename),
            refresh_rate=settings["REFRESH_RATE"],
        )

    @app.get("/api/display")
    def display():
        device_id = request.headers.get("ID", "")
        token = request.headers.get("Access-Token", "")
        if not _valid_device_id(device_id) or not token:
            return jsonify(error="Invalid device credentials."), 401
        mac = device_id.strip().upper()
        with _connect(settings["DATABASE_PATH"]) as connection:
            row = connection.execute("SELECT api_key FROM devices WHERE mac = ?", (mac,)).fetchone()
            if row is None or not hmac.compare_digest(row["api_key"], token):
                return jsonify(error="Invalid device credentials."), 401
            connection.execute("UPDATE devices SET last_seen_at = CURRENT_TIMESTAMP WHERE mac = ?", (mac,))
        filename = snapshot_image(settings["IMAGE_PATH"])
        return jsonify(
            status=0,
            image_url=image_url(filename),
            filename=filename,
            image_name=filename.removesuffix(".bmp"),
            refresh_rate=settings["REFRESH_RATE"],
            update_firmware=False,
            reset_firmware=False,
        )

    @app.get("/display/<filename>")
    def current_image(filename):
        if not re.fullmatch(r"[0-9a-f]{16}\.bmp", filename):
            abort(404)
        path = version_path(settings["IMAGE_PATH"], filename)
        if not path.is_file():
            abort(404)
        return send_file(path, mimetype="image/bmp", conditional=True, etag=filename)

    @app.post("/api/log")
    def device_log():
        return "", 204

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    return app
