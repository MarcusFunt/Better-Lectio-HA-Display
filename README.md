# Better Lectio HA Display

A small home-hosted TRMNL BYOS server for an 800×480 monochrome e-paper display. It combines a Lectio timetable with an optional calendar ICS feed, renders a bitmap in advance, and serves it to the device on the local network.

## Features

- Stock Seeed firmware support; no custom firmware build is required.
- Stable device enrollment and authenticated display polling backed by SQLite.
- Cache-busted 1-bit BMP output with immutable prior image URLs retained for seven days.
- Lectio schedule fetching through `python-lectio`, preserving weekday placement from the authenticated week grid.
- Optional ICS events, including all-day, recurring, moved, and cancelled instances.
- Separate systemd web and refresh services for Raspberry Pi OS Lite.

## Raspberry Pi Zero setup

Use Raspberry Pi OS Lite 32-bit. Keep the service on your home LAN; do not forward port 5000 from the internet.

```sh
sudo apt update
sudo apt install -y python3-venv fonts-dejavu-core
sudo useradd --system --home /opt/trmnl-display --shell /usr/sbin/nologin trmnl
sudo mkdir -p /opt/trmnl-display /var/lib/trmnl-display
sudo chown -R trmnl:trmnl /opt/trmnl-display /var/lib/trmnl-display
```

Clone the repository into `/opt/trmnl-display`, then install dependencies. Raspberry Pi OS configures piwheels for ARM wheels; it is also specified explicitly:

```sh
cd /opt/trmnl-display
sudo -u trmnl git clone https://github.com/MarcusFunt/Better-Lectio-HA-Display.git .
sudo -u trmnl python3 -m venv .venv
sudo -u trmnl .venv/bin/pip install --extra-index-url https://www.piwheels.org/simple -r requirements.txt
```

Create `/etc/trmnl-display.env` from `.env.example`; set `PUBLIC_BASE_URL` to a static LAN address or mDNS name resolvable by the display. Fill all three Lectio settings to enable timetable fetching, and set `CALENDAR_ICS_URL` to enable a calendar feed. Protect the file because a calendar URL may contain a private token:

```sh
sudo cp -n .env.example /etc/trmnl-display.env
sudo chown root:trmnl /etc/trmnl-display.env
sudo chmod 0640 /etc/trmnl-display.env
sudoedit /etc/trmnl-display.env
```

Install and start systemd units:

```sh
sudo install -m 0644 systemd/trmnl-display.service /etc/systemd/system/
sudo install -m 0644 systemd/trmnl-refresh.service /etc/systemd/system/
sudo install -m 0644 systemd/trmnl-refresh.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now trmnl-display.service trmnl-refresh.timer
sudo systemctl start trmnl-refresh.service
```

In the TRMNL setup portal, choose the custom server option and enter `http://<pi-address>:5000`. Check `http://<pi-address>:5000/health` from another LAN device.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `PUBLIC_BASE_URL` | Request host | Absolute LAN base URL returned to device |
| `REFRESH_RATE` | `1800` | Device poll interval, 60–21600 seconds |
| `TIMEZONE` | `Europe/Copenhagen` | Schedule display timezone |
| `DATABASE_PATH` | `data/devices.sqlite3` | Device registry |
| `IMAGE_PATH` | `data/current.bmp` | Current rendered image |
| `CACHE_PATH` | `data/events.json` | Cached normalized events |
| `LECTIO_USERNAME` | empty | Lectio username |
| `LECTIO_PASSWORD` | empty | Lectio password |
| `LECTIO_SCHOOL_ID` | empty | Numeric school ID |
| `LECTIO_CACHE_TTL_SECONDS` | `3600` | Lectio cache lifetime |
| `CALENDAR_ICS_URL` | empty | Optional ICS feed |
| `CALENDAR_CACHE_TTL_SECONDS` | `900` | Calendar cache lifetime |

The refresh timer runs every 15 minutes. Lectio is fetched at most once an hour by default; the ICS feed is fetched every 15 minutes. Cached events contain only one-way fingerprints of credential/feed identities. If a source fetch fails, the previous image remains in place. The refresh service has a 180-second startup timeout so a stalled SDK login cannot block later timer runs.

## API

- `GET /api/setup` and `/api/setup/`: firmware enrollment with `ID` header; setup JSON uses `status: 200`.
- `GET /api/display`: requires `ID` and `Access-Token`; success JSON uses `status: 0` and includes a content-derived filename, image URL, refresh rate, and firmware flags.
- `GET /display/<hash>.bmp`: serves immutable content-addressed images retained for seven days.
- `POST /api/log`: accepts diagnostics without persisting request content.
- `GET /health`: LAN health check.

## Development

```sh
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
```

To render once, run `python3 -m trmnl_schedule.refresh`. To serve locally, run `waitress-serve --listen=127.0.0.1:5000 --threads=1 trmnl_schedule.wsgi:app`.
