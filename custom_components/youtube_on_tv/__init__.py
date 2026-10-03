"""The YouTube on TV integration."""

from __future__ import annotations

from functools import partial

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator import YouTubeOnTvCoordinator
from .services import async_setup_services
from .session import DATA_SESSION, SessionTracker

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.MEDIA_PLAYER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TODO,
]

type YouTubeOnTvConfigEntry = ConfigEntry[YouTubeOnTvCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the session shared by all TVs, and the actions."""
    tracker = SessionTracker(hass)
    await tracker.async_load()
    hass.data[DATA_SESSION] = tracker
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: YouTubeOnTvConfigEntry) -> bool:
    """Set up YouTube on TV from a config entry."""
    coordinator = YouTubeOnTvCoordinator(hass, entry)
    await coordinator.async_start()
    entry.runtime_data = coordinator
    entry.async_on_unload(
        coordinator.async_add_listener(
            partial(hass.data[DATA_SESSION].async_observe, coordinator)
        )
    )
    # New open actions change the media player's features at once.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: YouTubeOnTvConfigEntry
) -> None:
    """Redraw the entities after the options change."""
    entry.runtime_data.async_update_listeners()


async def async_unload_entry(
    hass: HomeAssistant, entry: YouTubeOnTvConfigEntry
) -> bool:
    """Unload a config entry; the coordinator shuts down with it."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(
    hass: HomeAssistant, entry: YouTubeOnTvConfigEntry
) -> None:
    """Forget the removed TV's last session."""
    if DATA_SESSION in hass.data:
        hass.data[DATA_SESSION].async_forget(entry.entry_id)
