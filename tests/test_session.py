"""Tests for the last session: per-TV sensor, resume and transfer."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pyytlounge import NowPlayingEvent, State

from custom_components.youtube_on_tv.const import CONF_SCREEN_ID, DOMAIN
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .conftest import FakeLounge

A, B, C = "aqz-KE-bpKQ", "eRsGyueVLvQ", "R6MlUcmOul8"
PLAYER_ID = "media_player.youtube_on_samsung_neo_qled"
LAST_ID = "sensor.youtube_on_samsung_neo_qled_last_watched"
BEDROOM_PLAYER = "media_player.youtube_on_bedroom"
BEDROOM_LAST = "sensor.youtube_on_bedroom_last_watched"


async def _playing(
    hass: HomeAssistant,
    api: FakeLounge,
    entry: MockConfigEntry,
    queue: list[str],
    index: int = 1,
) -> None:
    """Report queue[index] playing at 100 s with the given queue."""
    await api.listener.now_playing_changed(
        NowPlayingEvent(
            {
                "videoId": queue[index],
                "currentTime": "100",
                "duration": "600",
                "state": str(State.Playing.value),
            }
        )
    )
    entry.runtime_data.handle_queue({"mdxExpandedReceiverVideoIdList": ",".join(queue)})
    await hass.async_block_till_done()


async def _two_tvs(
    hass: HomeAssistant,
    mock_lounge: list[FakeLounge],
    mock_config_entry: MockConfigEntry,
) -> tuple[MockConfigEntry, FakeLounge, MockConfigEntry, FakeLounge]:
    other = MockConfigEntry(
        domain=DOMAIN,
        title="Bedroom",
        unique_id="other-tv",
        data={CONF_SCREEN_ID: "f" * 64},
    )
    for entry in (mock_config_entry, other):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    async with asyncio.timeout(5):
        for api in mock_lounge:
            await api.subscribed.wait()
    first = next(
        api for api in mock_lounge if api.coordinator.config_entry is mock_config_entry
    )
    second = next(api for api in mock_lounge if api is not first)
    return mock_config_entry, first, other, second


async def test_session_recorded_and_resumed(
    hass: HomeAssistant,
    init_integration: FakeLounge,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The TV's sensor follows the video; resume plays it from where it stopped."""
    assert hass.states.get(LAST_ID).state == "unknown"
    await _playing(hass, init_integration, mock_config_entry, [A, B, C])
    state = hass.states.get(LAST_ID)
    assert state.state == "Dolor y Gloria"
    assert state.attributes["video_id"] == B
    assert state.attributes["playing"] is True
    assert state.attributes["queue_left"] == 1
    assert state.attributes["duration"] == 600

    freezer.tick(130)
    await init_integration.listener.disconnected(MagicMock())
    await hass.async_block_till_done()

    state = hass.states.get(LAST_ID)
    assert state.state != "unavailable"
    assert state.attributes["playing"] is False
    # Disconnect is the TV's last word: 100 s + 130 s played.
    assert state.attributes["position"] == 230
    assert state.attributes["url"] == f"https://www.youtube.com/watch?v={B}&t=230s"

    init_integration.set_playlist.reset_mock()
    await hass.services.async_call(DOMAIN, "resume", {}, blocking=True)
    init_integration.set_playlist.assert_awaited_once_with([A, B, C], 1, 230.0, "")


async def test_session_survives_restart(
    hass: HomeAssistant,
    init_integration: FakeLounge,
    mock_config_entry: MockConfigEntry,
    hass_storage,
) -> None:
    """A stopped session is saved at once, per TV."""
    await _playing(hass, init_integration, mock_config_entry, [B], index=0)
    await init_integration.listener.disconnected(MagicMock())
    await hass.async_block_till_done()
    data = hass_storage["youtube_on_tv.session"]["data"]
    assert data["session"]["video_id"] == B
    assert data["tvs"][mock_config_entry.entry_id]["video_id"] == B


async def test_session_loaded_at_start(
    hass: HomeAssistant,
    mock_lounge: list[FakeLounge],
    mock_app_state: AsyncMock,
    mock_config_entry: MockConfigEntry,
    hass_storage,
) -> None:
    """After a restart the sensor shows the saved session before any event."""
    session = {
        "entry_id": mock_config_entry.entry_id,
        "entity_id": PLAYER_ID,
        "device": "Samsung Neo QLED",
        "video_id": C,
        "title": "Saved",
        "channel": "Chan",
        "duration": 300.0,
        "position": 42.0,
        "position_updated_at": None,
        "status": "playing",
        "queue": [C],
        "queue_index": 0,
        "stopped_at": "2026-10-03T10:00:00+00:00",
    }
    hass_storage["youtube_on_tv.session"] = {
        "version": 1,
        "key": "youtube_on_tv.session",
        "data": {"session": session, "tvs": {mock_config_entry.entry_id: session}},
    }
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(LAST_ID)
    assert state.state == "Saved"
    assert state.attributes["position"] == 42
    assert state.attributes["queue_left"] == 0


async def test_resume_without_session(
    hass: HomeAssistant, init_integration: FakeLounge
) -> None:
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(DOMAIN, "resume", {}, blocking=True)
    assert err.value.translation_key == "no_session"


async def test_resume_rejects_other_entities(
    hass: HomeAssistant,
    init_integration: FakeLounge,
    mock_config_entry: MockConfigEntry,
) -> None:
    await _playing(hass, init_integration, mock_config_entry, [B], index=0)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "resume", {ATTR_ENTITY_ID: "sensor.anything"}, blocking=True
        )
    assert err.value.translation_key == "not_a_tv"


async def test_each_tv_keeps_its_own_session(
    hass: HomeAssistant,
    mock_lounge: list[FakeLounge],
    mock_app_state: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Each TV's sensor shows its own video; resume takes the latest by default."""
    entry, first, other, second = await _two_tvs(hass, mock_lounge, mock_config_entry)
    await _playing(hass, first, entry, [A, B])
    await _playing(hass, second, other, [C], index=0)
    assert hass.states.get(LAST_ID).attributes["video_id"] == B
    assert hass.states.get(BEDROOM_LAST).attributes["video_id"] == C

    await hass.services.async_call(DOMAIN, "resume", {}, blocking=True)
    second.set_playlist.assert_awaited_once()
    assert second.set_playlist.await_args.args[0] == [C]
    first.set_playlist.assert_not_awaited()

    # Resume on another TV plays the latest session there.
    await hass.services.async_call(
        DOMAIN, "resume", {ATTR_ENTITY_ID: PLAYER_ID}, blocking=True
    )
    assert first.set_playlist.await_args.args[0] == [C]


async def test_removed_tv_is_forgotten(
    hass: HomeAssistant,
    init_integration: FakeLounge,
    mock_config_entry: MockConfigEntry,
    hass_storage,
) -> None:
    await _playing(hass, init_integration, mock_config_entry, [B], index=0)
    await init_integration.listener.disconnected(MagicMock())
    await hass.async_block_till_done()
    assert await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    data = hass_storage["youtube_on_tv.session"]["data"]
    assert data == {"session": None, "tvs": {}}


async def test_transfer(
    hass: HomeAssistant,
    mock_lounge: list[FakeLounge],
    mock_app_state: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The video, position and queue move; the first TV pauses."""
    entry, first, _, second = await _two_tvs(hass, mock_lounge, mock_config_entry)
    await _playing(hass, first, entry, [A, B, C])

    await hass.services.async_call(
        DOMAIN,
        "transfer",
        {ATTR_ENTITY_ID: PLAYER_ID, "target": BEDROOM_PLAYER},
        blocking=True,
    )
    videos, index, position, list_id = second.set_playlist.await_args.args
    assert (videos, index, list_id) == ([A, B, C], 1, "")
    assert 100 <= position < 102
    first.pause.assert_awaited_once()

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "transfer",
            {ATTR_ENTITY_ID: PLAYER_ID, "target": PLAYER_ID},
            blocking=True,
        )
    assert err.value.translation_key == "same_tv"


async def test_transfer_needs_a_video(
    hass: HomeAssistant,
    mock_lounge: list[FakeLounge],
    mock_app_state: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    await _two_tvs(hass, mock_lounge, mock_config_entry)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "transfer",
            {ATTR_ENTITY_ID: PLAYER_ID, "target": BEDROOM_PLAYER},
            blocking=True,
        )
    assert err.value.translation_key == "no_video"
