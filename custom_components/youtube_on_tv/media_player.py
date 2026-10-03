"""Media player showing what the TV's YouTube app is playing."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    ATTR_MEDIA_ENQUEUE,
    ATTR_MEDIA_EXTRA,
    MediaPlayerDeviceClass,
    MediaPlayerEnqueue,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import YouTubeOnTvConfigEntry
from .const import DOMAIN
from .coordinator import PlayerStatus, YouTubeOnTvCoordinator
from .entity import YouTubeOnTvEntity

PARALLEL_UPDATES = 1

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# A YouTube list id: a Mix (RD...), a playlist (PL...) or another kind.
_LIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")
ATTR_LIST_ID = "list_id"
_YOUTUBE_HOSTS = {"youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}

_STATES = {
    PlayerStatus.OFF: MediaPlayerState.OFF,
    PlayerStatus.IDLE: MediaPlayerState.IDLE,
    PlayerStatus.PLAYING: MediaPlayerState.PLAYING,
    PlayerStatus.PAUSED: MediaPlayerState.PAUSED,
    PlayerStatus.BUFFERING: MediaPlayerState.BUFFERING,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: YouTubeOnTvConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the media player."""
    async_add_entities([YouTubeOnTvMediaPlayer(entry.runtime_data)])


class YouTubeOnTvMediaPlayer(YouTubeOnTvEntity, MediaPlayerEntity):
    """The TV's YouTube app."""

    _attr_name = None
    _attr_device_class = MediaPlayerDeviceClass.TV
    _attr_media_content_type = MediaType.VIDEO
    _attr_media_image_remotely_accessible = True
    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.SEEK
        | MediaPlayerEntityFeature.NEXT_TRACK
        | MediaPlayerEntityFeature.PREVIOUS_TRACK
        | MediaPlayerEntityFeature.PLAY_MEDIA
        | MediaPlayerEntityFeature.MEDIA_ENQUEUE
    )

    def __init__(self, coordinator: YouTubeOnTvCoordinator) -> None:
        """Initialize the media player."""
        super().__init__(coordinator, "media_player")
        if coordinator.has_app_state:
            # Opening and closing the app is done over DIAL, so it needs the
            # TV's address, which a TV added with a code doesn't have.
            self._attr_supported_features |= (
                MediaPlayerEntityFeature.TURN_ON | MediaPlayerEntityFeature.TURN_OFF
            )

    @property
    def state(self) -> MediaPlayerState:
        """Return the playback state."""
        return _STATES[self.coordinator.data.status]

    @property
    def media_content_id(self) -> str | None:
        """Return the YouTube video id."""
        return self.coordinator.data.video_id

    @property
    def media_title(self) -> str | None:
        """Return the video title."""
        return self.coordinator.data.title

    @property
    def media_artist(self) -> str | None:
        """Return the channel name."""
        return self.coordinator.data.channel

    @property
    def media_image_url(self) -> str | None:
        """Return the video thumbnail."""
        return self.coordinator.data.thumbnail_url

    @property
    def media_duration(self) -> int | None:
        """Return the video duration in seconds."""
        duration = self.coordinator.data.duration
        return round(duration) if duration else None

    @property
    def media_position(self) -> int | None:
        """Return the playback position in seconds."""
        position = self.coordinator.data.position
        return round(position) if position is not None else None

    @property
    def media_position_updated_at(self) -> datetime | None:
        """Return when the position was last reported."""
        return self.coordinator.data.position_updated_at

    async def async_media_play(self) -> None:
        """Resume playback."""
        await self.coordinator.async_command(self.coordinator.api.play)

    async def async_media_pause(self) -> None:
        """Pause playback."""
        await self.coordinator.async_command(self.coordinator.api.pause)

    async def async_media_seek(self, position: float) -> None:
        """Seek to a position in seconds."""
        await self.coordinator.async_command(self.coordinator.api.seek_to, position)

    async def async_media_next_track(self) -> None:
        """Play the next video."""
        await self.coordinator.async_command(self.coordinator.api.next)

    async def async_media_previous_track(self) -> None:
        """Play the previous video."""
        await self.coordinator.async_command(self.coordinator.api.previous)

    async def async_play_media(
        self, media_type: str, media_id: str, **kwargs: Any
    ) -> None:
        """Play or queue a YouTube video, given its id or URL.

        enqueue "add" appends it to the queue, "next" plays it after the
        current video, "replace" clears the queue; "play" or none plays it now.
        A media type of "playlist" takes a comma list of video ids instead.
        """
        if media_source.is_media_source_id(media_id):
            # A media source (another integration's media browser) resolves
            # to a video id, or to a comma list of ids for a playlist.
            resolved = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id
            )
            media_id = resolved.url
            media_type = (
                MediaType.PLAYLIST
                if resolved.mime_type == MediaType.PLAYLIST
                else MediaType.VIDEO
            )
        if media_type == MediaType.PLAYLIST:
            await self._async_play_video_list(media_id, **kwargs)
            return
        video_id = parse_video_id(media_id)
        if video_id is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_video",
                translation_placeholders={"media_id": media_id},
            )
        if self.coordinator.app_running is False:
            # YouTube isn't open: a Lounge command would be accepted by
            # YouTube's servers and never reach the TV, so open the app on it.
            await self.coordinator.async_launch(video_id)
            return
        enqueue = kwargs.get(ATTR_MEDIA_ENQUEUE)
        if enqueue == MediaPlayerEnqueue.ADD:
            await self.coordinator.async_queue_add(video_id)
        elif enqueue == MediaPlayerEnqueue.NEXT:
            await self.coordinator.async_queue_next(video_id)
        elif enqueue == MediaPlayerEnqueue.REPLACE:
            await self.coordinator.async_queue_replace([video_id])
        else:
            await self.coordinator.async_command(
                self.coordinator.api.play_video, video_id
            )

    async def _async_play_video_list(self, media_id: str, **kwargs: Any) -> None:
        """Play or queue a comma list of video ids as one queue.

        The list goes to the TV as one setPlaylist, which is much faster than
        one addVideo per video. With a list id in extra (a Mix or playlist
        the ids came from), the TV keeps the Mix going after the last video,
        as it does when the phone app casts one.
        """
        video_ids = parse_video_list(media_id)
        if video_ids is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_video_list",
                translation_placeholders={"media_id": media_id},
            )
        extra = kwargs.get(ATTR_MEDIA_EXTRA) or {}
        list_id = str(extra.get(ATTR_LIST_ID) or "")
        if list_id and not _LIST_ID_RE.match(list_id):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_list_id",
                translation_placeholders={"list_id": list_id},
            )
        enqueue = kwargs.get(ATTR_MEDIA_ENQUEUE)
        if enqueue in (MediaPlayerEnqueue.ADD, MediaPlayerEnqueue.NEXT):
            await self.coordinator.async_queue_extend(
                video_ids, after_current=enqueue == MediaPlayerEnqueue.NEXT
            )
        else:
            await self.coordinator.async_play_list(video_ids, list_id=list_id)

    async def async_turn_on(self) -> None:
        """Open YouTube on the TV."""
        await self.coordinator.async_launch()

    async def async_turn_off(self) -> None:
        """Close YouTube on the TV."""
        await self.coordinator.async_stop()


def parse_video_id(media_id: str) -> str | None:
    """Return the video id from a YouTube video id or URL."""
    media_id = media_id.strip()
    if _VIDEO_ID_RE.match(media_id):
        return media_id
    url = urlparse(media_id if "://" in media_id else f"https://{media_id}")
    host = (url.hostname or "").removeprefix("www.")
    if host not in _YOUTUBE_HOSTS:
        return None
    if host == "youtu.be":
        candidate = url.path.strip("/")
    elif url.path.startswith(("/shorts/", "/live/", "/embed/")):
        candidate = url.path.split("/")[2]
    else:
        candidate = parse_qs(url.query).get("v", [""])[0]
    return candidate if _VIDEO_ID_RE.match(candidate) else None


def parse_video_list(media_id: str) -> list[str] | None:
    """Return the video ids of a comma list, or None if one isn't an id."""
    video_ids = [part.strip() for part in media_id.split(",") if part.strip()]
    if not video_ids or not all(_VIDEO_ID_RE.match(v) for v in video_ids):
        return None
    return video_ids
