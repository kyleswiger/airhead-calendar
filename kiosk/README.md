# Kiosk provisioning

Target for M5. The display device (HP Spectre 16 now, Raspberry Pi later) runs a minimal
Debian install with Chromium in kiosk mode pointed at https://airhead.kswiger.dev.

Because the display is just a browser at a URL, moving to a Pi is reimaging one device -
no API or application changes.

Planned contents:
- `provision.sh` - one-shot device setup
- `airhead-kiosk.service` - systemd unit, Chromium `--kiosk --noerrdialogs`
- `airhead-watchdog.service` - restart on crash, health beacon to the API
- `power.sh` - dim after 2 min idle, sleep after 30
