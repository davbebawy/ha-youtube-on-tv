# Development

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/pytest --cov
```

The Home Assistant test harness only runs on Linux and macOS. On Windows, use WSL.

## Diagnostic scripts

Two standalone tools help when a device behaves differently from the ones already covered. Both only read, and neither prints the screen id.

### dial_scan.py

Shows which devices publish DIAL on the network, where, and whether their YouTube app exposes a screen id. Run it with YouTube open on the device:

```bash
python scripts/dial_scan.py                      # search the network
python scripts/dial_scan.py --host 192.168.1.50  # try known addresses on one device
```

A device that answers but reports no YouTube app can only be added with a TV code; that's what Android TV devices do.

### lounge_probe.py

Prints the events a TV sends, which is how the behavior documented in [How it behaves](behavior.md) was worked out. `--debug` also logs events the library doesn't handle:

```bash
.venv/bin/python scripts/lounge_probe.py --host 192.168.1.50 --seconds 300 --debug
```

It tries the DIAL addresses of common devices. Pass `--app-url` with whatever `dial_scan.py` reports for an unusual one, or `--pair-code` with a TV code for a device that publishes no YouTube app over DIAL.

## Reporting a new device

If you have a device not listed in [supported devices](../README.md#supported-devices), the output of `dial_scan.py` and a few minutes of `lounge_probe.py`, posted in the [community thread](https://community.home-assistant.io/t/youtube-on-tv-see-and-control-what-the-youtube-app-on-your-tv-is-playing/1026146), is what it takes to add support or document its limits.
