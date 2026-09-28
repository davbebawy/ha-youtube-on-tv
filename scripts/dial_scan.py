"""Scan the network for DIAL devices and report their YouTube endpoints.

Useful when a TV isn't discovered by Home Assistant, or can't be added by IP
address: it shows where the device publishes DIAL and whether its YouTube app
exposes a screen id.

No screen id is printed, only whether one is there.

Usage: python scripts/dial_scan.py [--host TV_IP] [--seconds 5]
"""

import argparse
import re
import socket
import sys
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

DIAL_ST = "urn:dial-multiscreen-org:service:dial:1"
ORIGIN = "https://www.youtube.com"
TIMEOUT = 5

# Where DIAL servers are known to publish their device description.
KNOWN_DESCRIPTION_URLS = (
    "http://{host}:7678/nservice/",  # Samsung Tizen
    "http://{host}:8008/ssdp/device-desc.xml",  # Chromecast, Google/Android TV
)


def search(seconds: int) -> set[str]:
    """Return the description URLs of DIAL devices answering on the network."""
    message = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: 2\r\nST: {DIAL_ST}\r\n\r\n"
    ).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(seconds)
    sock.sendto(message, ("239.255.255.250", 1900))
    locations: set[str] = set()
    try:
        while True:
            data, _addr = sock.recvfrom(4096)
            found = re.search(r"(?im)^location:\s*(\S+)", data.decode(errors="ignore"))
            if found:
                locations.add(found.group(1))
    except TimeoutError:
        pass
    finally:
        sock.close()
    return locations


def get(url: str) -> tuple[int, dict[str, str], str]:
    """Fetch a URL, returning its status, headers and body."""
    request = Request(url, headers={"Origin": ORIGIN})  # noqa: S310
    with urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
        return (
            response.status,
            dict(response.headers),
            response.read().decode(errors="ignore"),
        )


def tag(xml: str, name: str) -> str | None:
    """Return the text of the first <name> element."""
    found = re.search(rf"<{name}>\s*([^<]*?)\s*</{name}>", xml)
    return found.group(1) if found and found.group(1) else None


def report(location: str) -> None:
    """Print what a DIAL device says about itself and its YouTube app."""
    print(f"\n=== {location}")
    try:
        _status, headers, body = get(location)
    except Exception as err:
        print(f"  could not read the description: {err}")
        return

    app_url = headers.get("Application-URL")
    print(f"  name:            {tag(body, 'friendlyName')}")
    print(f"  manufacturer:    {tag(body, 'manufacturer')}")
    print(f"  model:           {tag(body, 'modelName')}")
    print(f"  Application-URL: {app_url}")
    if not app_url:
        print("  not a DIAL server (no Application-URL header)")
        return

    youtube_url = urljoin(
        app_url if app_url.endswith("/") else f"{app_url}/", "YouTube"
    )
    try:
        status, _headers, app = get(youtube_url)
    except Exception as err:
        print(f"  YouTube app info failed: {youtube_url}: {err}")
        return
    screen_id = tag(app, "screenId")
    print(f"  YouTube app url: {youtube_url} (HTTP {status})")
    print(f"  app state:       {tag(app, 'state')}")
    print(
        f"  screen id:       {'yes, ' + str(len(screen_id)) + ' chars' if screen_id else 'NOT PUBLISHED'}"
    )
    if not screen_id:
        print("  -> open YouTube on the device and run this again")


def main() -> int:
    """Scan the network, or probe one host."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", help="probe only this IP address or hostname")
    parser.add_argument("--seconds", type=int, default=5, help="how long to scan")
    args = parser.parse_args()

    if args.host:
        print(f"Trying known DIAL addresses on {args.host} ...")
        for template in KNOWN_DESCRIPTION_URLS:
            report(template.format(host=args.host))
        return 0

    print(f"Searching for DIAL devices for {args.seconds}s ...")
    locations = search(args.seconds)
    if not locations:
        print(
            "No DIAL devices answered. The device may be on another network, or "
            "this computer may be on a different subnet or VLAN than the TV."
        )
        return 1
    print(f"Found {len(locations)}: {', '.join(urlparse(u).netloc for u in locations)}")
    for location in sorted(locations):
        report(location)
    return 0


if __name__ == "__main__":
    sys.exit(main())
