"""Feasibility probe: listen to a TV's YouTube Lounge session without manual pairing.

Steps:
  1. Read the screenId from the TV's DIAL endpoint (YouTube app info).
  2. Exchange it for a lounge token (pair_with_screen_id).
  3. Connect as a remote and print now-playing / state events for a while.

The screenId and lounge token are credentials, so they are never printed.

Usage: python scripts/lounge_probe.py --host TV_IP [--seconds 120] [--debug]
       python scripts/lounge_probe.py --pair-code 123456789012   (no DIAL needed)
"""

import argparse
import asyncio
import logging
import re
import sys
import time

import aiohttp
from pyytlounge import (
    AdPlayingEvent,
    AdStateEvent,
    AutoplayModeChangedEvent,
    AutoplayUpNextEvent,
    DisconnectedEvent,
    EventListener,
    NowPlayingEvent,
    PlaybackSpeedEvent,
    PlaybackStateEvent,
    SubtitlesTrackEvent,
    VolumeChangedEvent,
    YtLoungeApi,
)

DEVICE_NAME = "HA Lounge Probe"

# YouTube app URLs of common DIAL servers. Use --app-url for anything else;
# scripts/dial_scan.py reports what a device actually publishes.
KNOWN_APP_URLS = (
    "http://{host}:8080/ws/app/YouTube",  # Samsung Tizen
    "http://{host}:8008/apps/YouTube",  # Chromecast, Google/Android TV
    "http://{host}:8060/dial/YouTube",  # Roku
    "http://{host}:36866/apps/YouTube",  # LG webOS
)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def get_screen_id(
    session: aiohttp.ClientSession, host: str, app_url: str | None
) -> str:
    urls = [app_url] if app_url else [u.format(host=host) for u in KNOWN_APP_URLS]
    body = None
    missing = False
    for url in urls:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    log(f"DIAL: {url} returned HTTP {resp.status}")
                    missing = missing or resp.status == 404
                    continue
                body = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            log(f"DIAL: {url}: {err}")
            continue
        log(f"DIAL: using {url}")
        break

    if body is None and missing:
        raise RuntimeError(
            "The device's DIAL server has no YouTube app, so there is no screen "
            "id to read. Run again with --pair-code and the code from the TV "
            "(YouTube > Settings > Link with TV code)."
        )
    if body is None:
        raise RuntimeError(
            "No YouTube app found over DIAL. Open YouTube on the device, or pass "
            "--app-url with the address scripts/dial_scan.py reports."
        )

    state = re.search(r"<state>(.*?)</state>", body)
    log(f"DIAL: YouTube app state = {state.group(1) if state else '?'}")
    match = re.search(r"<screenId>(.*?)</screenId>", body)
    if not match:
        raise RuntimeError("DIAL response has no screenId (is the YouTube app open?)")
    return match.group(1)


class TitleCache:
    """Resolves video titles through YouTube oEmbed (no API key needed)."""

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session
        self._cache: dict[str, str] = {}

    async def get(self, video_id: str) -> str:
        if video_id not in self._cache:
            url = "https://www.youtube.com/oembed"
            params = {
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "format": "json",
            }
            try:
                async with self._session.get(url, params=params) as resp:
                    data = await resp.json(content_type=None)
                    self._cache[video_id] = f"{data['title']} — {data['author_name']}"
            except Exception as err:
                self._cache[video_id] = f"<title lookup failed: {err}>"
        return self._cache[video_id]


def fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


class PrintListener(EventListener):
    def __init__(self, titles: TitleCache):
        super().__init__()
        self._titles = titles

    async def now_playing_changed(self, event: NowPlayingEvent) -> None:
        title = await self._titles.get(event.video_id) if event.video_id else "-"
        log(
            f"NOW PLAYING  video={event.video_id} state={event.state.name} "
            f"pos={fmt_time(event.current_time)}/{fmt_time(event.duration)}  {title}"
        )

    async def playback_state_changed(self, event: PlaybackStateEvent) -> None:
        log(
            f"STATE        {event.state.name} "
            f"pos={fmt_time(event.current_time)}/{fmt_time(event.duration)}"
        )

    async def volume_changed(self, event: VolumeChangedEvent) -> None:
        log(f"VOLUME       {event.volume} muted={event.muted}")

    async def ad_state_changed(self, event: AdStateEvent) -> None:
        log(f"AD STATE     {vars(event)}")

    async def ad_playing_changed(self, event: AdPlayingEvent) -> None:
        log(f"AD PLAYING   {getattr(event, 'ad_title', '')}")

    async def playback_speed_changed(self, event: PlaybackSpeedEvent) -> None:
        log(f"SPEED        {vars(event)}")

    async def autoplay_changed(self, event: AutoplayModeChangedEvent) -> None:
        log(f"AUTOPLAY     enabled={event.enabled} supported={event.supported}")

    async def autoplay_up_next_changed(self, event: AutoplayUpNextEvent) -> None:
        title = await self._titles.get(event.video_id) if event.video_id else "-"
        log(f"UP NEXT      video={event.video_id}  {title}")

    async def subtitles_track_changed(self, event: SubtitlesTrackEvent) -> None:
        log(f"SUBTITLES    {vars(event)}")

    async def disconnected(self, event: DisconnectedEvent) -> None:
        log(f"DISCONNECTED {vars(event)}")


async def listen(
    api: YtLoungeApi, screen_id: str | None, pair_code: str | None = None
) -> None:
    """Keep the lounge session alive: refresh auth, reconnect, re-subscribe."""
    while True:
        if not api.linked():
            if screen_id:
                log("Linking with screenId ...")
                linked = await api.pair_with_screen_id(screen_id)
            else:
                log("Pairing with the TV code ...")
                try:
                    linked = await api.pair(pair_code)
                except (
                    aiohttp.ClientResponseError,
                    KeyError,
                    TypeError,
                    ValueError,
                ) as err:
                    raise RuntimeError(
                        "The TV code was not accepted. Codes expire after a "
                        "few minutes; get a fresh one from the TV."
                    ) from err
                # Codes are single use; reconnect with the screen id it gave.
                screen_id = api.auth.screen_id
            if not linked:
                raise RuntimeError("YouTube did not return a lounge token")
            log("Linked (lounge token obtained)")
        if not api.connected():
            log("Connecting ...")
            if not await api.connect():
                log("Connect failed, retrying in 5s")
                await asyncio.sleep(5)
                continue
            log(f"Connected to screen '{api.screen_name}' ({api.screen_device_name})")
            await api.get_now_playing()
        await api.subscribe()  # long-poll; returns when the server ends the request


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", help="TV IP address or hostname")
    parser.add_argument(
        "--pair-code",
        help="TV code, for devices whose DIAL server has no YouTube app",
    )
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument(
        "--app-url",
        help="YouTube DIAL app URL, if the device isn't at a known address",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="log raw events, including ones the library doesn't handle",
    )
    args = parser.parse_args()

    # pyytlounge logs the lounge token at INFO, so keep its logger at WARNING
    # unless raw events are wanted.
    lib_logger = logging.getLogger("pyytlounge")
    lib_logger.setLevel(logging.DEBUG if args.debug else logging.WARNING)
    logging.basicConfig(format="[lib] %(levelname)s %(message)s")

    if not args.host and not args.pair_code:
        parser.error("pass --host, or --pair-code for a device without DIAL")

    async with aiohttp.ClientSession() as session:
        screen_id = None
        if not args.pair_code:
            try:
                screen_id = await get_screen_id(session, args.host, args.app_url)
            except RuntimeError as err:
                log(str(err))
                return 1
            log(f"DIAL: screenId found ({len(screen_id)} chars)")

        async with YtLoungeApi(
            DEVICE_NAME, PrintListener(TitleCache(session)), lib_logger
        ) as api:
            log(f"Listening for {args.seconds}s — play/pause/seek from your phone now")
            try:
                await asyncio.wait_for(
                    listen(api, screen_id, args.pair_code), timeout=args.seconds
                )
            except TimeoutError:
                log("Time is up")
            except RuntimeError as err:
                log(str(err))
                return 1
            finally:
                if api.connected():
                    await api.disconnect()
                    log("Disconnected cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
