"""Actions: resume the last session, and move playback to another TV."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er

from .const import DOMAIN, LOGGER
from .coordinator import YouTubeOnTvCoordinator, current_position
from .session import DATA_SESSION, resume_position

SERVICE_RESUME = "resume"
SERVICE_TRANSFER = "transfer"
ATTR_TARGET = "target"

RESUME_SCHEMA = vol.Schema({vol.Optional(ATTR_ENTITY_ID): cv.entity_id})
TRANSFER_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required(ATTR_TARGET): cv.entity_id,
    }
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration's actions."""

    async def resume(call: ServiceCall) -> None:
        session = hass.data[DATA_SESSION].session
        if session is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_session"
            )
        target = _tv(hass, call.data.get(ATTR_ENTITY_ID) or session.get("entity_id"))
        queue: list[str] = session.get("queue") or []
        index = session.get("queue_index")
        if index is None or index >= len(queue) or queue[index] != session["video_id"]:
            queue, index = [session["video_id"]], 0
        await target.async_play_list(queue, index, resume_position(session))

    async def transfer(call: ServiceCall) -> None:
        source = _tv(hass, call.data[ATTR_ENTITY_ID])
        target = _tv(hass, call.data[ATTR_TARGET])
        if source is target:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="same_tv"
            )
        state = source.working_state
        if state.video_id is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_video"
            )
        queue, index = list(state.queue), state.queue_index
        if index is None or not queue:
            queue, index = [state.video_id], 0
        await target.async_play_list(queue, index, current_position(state))
        try:
            await source.async_command(source.api.pause)
        except HomeAssistantError as err:
            # Moved already; pausing the first TV is a courtesy.
            LOGGER.debug(
                "Error pausing %s after the move: %s", call.data[ATTR_ENTITY_ID], err
            )

    hass.services.async_register(DOMAIN, SERVICE_RESUME, resume, RESUME_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_TRANSFER, transfer, TRANSFER_SCHEMA)


def _tv(hass: HomeAssistant, entity_id: str | None) -> YouTubeOnTvCoordinator:
    """Return the TV behind a YouTube on TV media player."""
    entity = er.async_get(hass).async_get(entity_id) if entity_id else None
    entry = (
        hass.config_entries.async_get_entry(entity.config_entry_id)
        if entity is not None
        and entity.platform == DOMAIN
        and entity.domain == "media_player"
        and entity.config_entry_id
        else None
    )
    if entry is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_a_tv",
            translation_placeholders={"entity_id": str(entity_id)},
        )
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="tv_not_loaded",
            translation_placeholders={"entity_id": str(entity_id)},
        )
    return entry.runtime_data
