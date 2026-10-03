"""Tests for playing a list of video ids, and media source ids."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pyytlounge import NowPlayingEvent, State

from custom_components.youtube_on_tv.media_player import parse_video_list
from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_ENQUEUE,
    ATTR_MEDIA_EXTRA,
    DOMAIN as MP_DOMAIN,
    SERVICE_PLAY_MEDIA,
)
from homeassistant.components.media_source import PlayMedia
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .conftest import FakeLounge

PLAYER_ID = "media_player.youtube_on_samsung_neo_qled"
A, B, C, D = "aqz-KE-bpKQ", "eRsGyueVLvQ", "R6MlUcmOul8", "jNQXAC9IVRw"
QUEUE_LIST_ID = "RQKYQmR-w0UahoeD2K7QcARqr3DiA"
MIX = "RDqyUPz6_TciY"
RESOLVE = (
    "custom_components.youtube_on_tv.media_player.media_source.async_resolve_media"
)


async def _playing(
    hass: HomeAssistant,
    api: FakeLounge,
    entry: MockConfigEntry,
    video_id: str,
    queue: list[str] | None,
) -> None:
    await api.listener.now_playing_changed(
        NowPlayingEvent(
            {
                "videoId": video_id,
                "currentTime": "100",
                "duration": "600",
                "state": str(State.Playing.value),
            }
        )
    )
    if queue is not None:
        entry.runtime_data.handle_queue(
            {
                "listId": QUEUE_LIST_ID,
                "mdxExpandedReceiverVideoIdList": ",".join(queue),
            }
        )
    await hass.async_block_till_done()


async def _play_list(
    hass: HomeAssistant,
    media_id: str,
    enqueue: str | None = None,
    extra: dict | None = None,
    media_type: str = "playlist",
) -> None:
    data = {
        ATTR_ENTITY_ID: PLAYER_ID,
        ATTR_MEDIA_CONTENT_ID: media_id,
        ATTR_MEDIA_CONTENT_TYPE: media_type,
    }
    if enqueue:
        data[ATTR_MEDIA_ENQUEUE] = enqueue
    if extra is not None:
        data[ATTR_MEDIA_EXTRA] = extra
    await hass.services.async_call(MP_DOMAIN, SERVICE_PLAY_MEDIA, data, blocking=True)


def test_parse_video_list() -> None:
    assert parse_video_list(f"{A}, {B},{C},") == [A, B, C]
    assert parse_video_list(A) == [A]
    assert parse_video_list(f"{A},https://youtu.be/{B}") is None
    assert parse_video_list("") is None
    assert parse_video_list(f"{A},short") is None


@pytest.mark.parametrize("enqueue", [None, "play", "replace"])
async def test_list_plays_as_one_queue(
    hass: HomeAssistant, init_integration: FakeLounge, enqueue: str | None
) -> None:
    """A comma list is one setPlaylist, from its first video."""
    await _play_list(hass, f"{A},{B},{C}", enqueue)
    init_integration.set_playlist.assert_awaited_once_with([A, B, C], 0, 0, "")
    init_integration.play_video.assert_not_awaited()
    init_integration.add_video.assert_not_awaited()


async def test_list_id_keeps_the_mix_going(
    hass: HomeAssistant, init_integration: FakeLounge
) -> None:
    """A Mix's list id goes along as listId."""
    await _play_list(hass, f"{A},{B}", extra={"list_id": MIX})
    init_integration.set_playlist.assert_awaited_once_with([A, B], 0, 0, MIX)


@pytest.mark.parametrize(
    ("enqueue", "expected"),
    [("add", [A, B, C, D]), ("next", [A, C, D, B])],
)
async def test_list_queued_after_the_playing_video(
    hass: HomeAssistant,
    init_integration: FakeLounge,
    mock_config_entry: MockConfigEntry,
    enqueue: str,
    expected: list[str],
) -> None:
    """add and next put the list in the TV's queue; the video keeps playing."""
    await _playing(hass, init_integration, mock_config_entry, A, [A, B])
    await _play_list(hass, f"{C},{D}", enqueue, extra={"list_id": MIX})
    videos, index, position, list_id = init_integration.set_playlist.await_args.args
    assert (videos, index) == (expected, 0)
    assert position >= 100
    # The queue keeps its own id; a Mix id only applies to a new queue.
    assert list_id == QUEUE_LIST_ID
    assert mock_config_entry.runtime_data.data.queue == tuple(expected)


async def test_list_next_without_known_queue_keeps_the_video(
    hass: HomeAssistant,
    init_integration: FakeLounge,
    mock_config_entry: MockConfigEntry,
) -> None:
    """With no queue reported, the playing video heads the new queue."""
    await _playing(hass, init_integration, mock_config_entry, A, None)
    await _play_list(hass, f"{B},{C}", "next")
    videos, index, position, list_id = init_integration.set_playlist.await_args.args
    assert (videos, index, list_id) == ([A, B, C], 0, "")
    assert position >= 100


async def test_list_add_plays_when_nothing_plays(
    hass: HomeAssistant, init_integration: FakeLounge
) -> None:
    await _play_list(hass, f"{B},{C}", "add")
    init_integration.set_playlist.assert_awaited_once_with([B, C], 0, 0, "")


@pytest.mark.parametrize(
    "media_id", [f"{A},not-an-id", "", f"https://youtu.be/{A}", "RDqyUPz6_TciY"]
)
async def test_list_rejects_non_ids(
    hass: HomeAssistant, init_integration: FakeLounge, media_id: str
) -> None:
    """Only video ids: a Mix or playlist id needs its videos expanded first."""
    with pytest.raises(ServiceValidationError) as err:
        await _play_list(hass, media_id)
    assert err.value.translation_key == "invalid_video_list"
    init_integration.set_playlist.assert_not_awaited()


async def test_list_rejects_a_bad_list_id(
    hass: HomeAssistant, init_integration: FakeLounge
) -> None:
    with pytest.raises(ServiceValidationError) as err:
        await _play_list(hass, A, extra={"list_id": "no spaces allowed"})
    assert err.value.translation_key == "invalid_list_id"
    init_integration.set_playlist.assert_not_awaited()


async def test_media_source_video(
    hass: HomeAssistant, init_integration: FakeLounge
) -> None:
    """A media source id resolving to a video id plays that video."""
    with patch(
        RESOLVE, AsyncMock(return_value=PlayMedia(url=C, mime_type="video"))
    ) as resolve:
        await _play_list(
            hass, "media-source://youtube_account/feed/" + C, media_type="video"
        )
    resolve.assert_awaited_once()
    assert resolve.await_args.args[1] == "media-source://youtube_account/feed/" + C
    init_integration.play_video.assert_awaited_once_with(C)


async def test_media_source_playlist(
    hass: HomeAssistant, init_integration: FakeLounge
) -> None:
    """A media source id resolving to a playlist plays the ids as one queue."""
    with patch(
        RESOLVE, AsyncMock(return_value=PlayMedia(url=f"{A},{B}", mime_type="playlist"))
    ):
        await _play_list(
            hass, "media-source://youtube_account/mix/" + MIX, media_type="video"
        )
    init_integration.set_playlist.assert_awaited_once_with([A, B], 0, 0, "")
