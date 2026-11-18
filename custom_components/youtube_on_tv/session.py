"""The last watching session, kept so it can be resumed on any TV."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import TvState, YouTubeOnTvCoordinator

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.session"
# While a video plays the session is saved at most this often, so it
# survives a Home Assistant restart.
SAVE_DELAY = 30
# Position changes smaller than this don't notify listeners.
POSITION_STEP = 5.0

DATA_SESSION: HassKey[SessionTracker] = HassKey(f"{DOMAIN}_session")


class SessionTracker:
    """Follows every TV and remembers the last video watched.

    A session is the video, position and queue of a TV. Each TV keeps its
    own; session is the one of the TV that played last, which resume plays
    by default. A session is updated while its TV plays and marked stopped
    when the TV stops.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the tracker."""
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.session: dict[str, Any] | None = None
        # entry id -> the last session of that TV.
        self.tvs: dict[str, dict[str, Any]] = {}
        # entry id -> session of a TV that has a video now.
        self._live: dict[str, dict[str, Any]] = {}
        self._listeners: list[Callable[[], None]] = []

    async def async_load(self) -> None:
        """Load the saved session."""
        data = await self._store.async_load() or {}
        self.session = data.get("session")
        self.tvs = data.get("tvs") or {}

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call listener when the session changes."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def async_observe(self, coordinator: YouTubeOnTvCoordinator) -> None:
        """Record a TV's published state."""
        entry = coordinator.config_entry
        state = coordinator.data
        live = self._live.get(entry.entry_id)
        if state.video_id is not None:
            snapshot = _snapshot(self.hass, coordinator, state, live)
            self._live[entry.entry_id] = snapshot
            if not _same(self.tvs.get(entry.entry_id), snapshot):
                self._set(snapshot, immediate=False)
            return
        if live is None:
            return
        del self._live[entry.entry_id]
        self._set(
            {
                **live,
                "position": _position_at(live, coordinator.last_event_at),
                "status": "stopped",
                "stopped_at": dt_util.utcnow().isoformat(),
            },
            immediate=True,
        )

    @callback
    def async_forget(self, entry_id: str) -> None:
        """Drop the session of a TV that was removed."""
        self._live.pop(entry_id, None)
        if self.tvs.pop(entry_id, None) is None:
            return
        if self.session is not None and self.session.get("entry_id") == entry_id:
            self.session = None
        self.hass.async_create_task(self._store.async_save(self._data()))
        self._notify()

    @callback
    def _set(self, session: dict[str, Any], *, immediate: bool) -> None:
        self.session = session
        self.tvs[session["entry_id"]] = session
        if immediate:
            self.hass.async_create_task(self._store.async_save(self._data()))
        else:
            self._store.async_delay_save(self._data, SAVE_DELAY)
        self._notify()

    def _data(self) -> dict[str, Any]:
        return {"session": self.session, "tvs": self.tvs}

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()


def _snapshot(
    hass: HomeAssistant,
    coordinator: YouTubeOnTvCoordinator,
    state: TvState,
    live: dict[str, Any] | None,
) -> dict[str, Any]:
    entry = coordinator.config_entry
    position = state.position
    updated_at = state.position_updated_at
    if position is None and live is not None and live["video_id"] == state.video_id:
        # During an ad the video's position is unknown; keep the last one.
        position = live["position"]
        updated_at = None
    return {
        "entry_id": entry.entry_id,
        "entity_id": er.async_get(hass).async_get_entity_id(
            "media_player", DOMAIN, f"{entry.unique_id}_media_player"
        ),
        "device": entry.title,
        "video_id": state.video_id,
        "title": state.title,
        "channel": state.channel,
        "duration": state.duration,
        "position": position,
        "position_updated_at": updated_at.isoformat() if updated_at else None,
        "status": str(state.status),
        "queue": list(state.queue),
        "queue_index": state.queue_index,
        "stopped_at": None,
    }


def _position_at(
    session: dict[str, Any], last_event_at: datetime | None
) -> float | None:
    """Return where a playing video was when the TV was last heard from.

    The TV often stops without a word: its last event is the latest time it's
    known to have played, so the position is extrapolated to then, not now.
    """
    position = session.get("position")
    updated = session.get("position_updated_at")
    if position is None or session.get("status") != "playing" or updated is None:
        return position
    if last_event_at is None:
        return position
    elapsed = (last_event_at - dt_util.parse_datetime(updated)).total_seconds()
    position += max(elapsed, 0)
    if duration := session.get("duration"):
        position = min(position, duration)
    return round(position, 1)


def _same(old: dict[str, Any] | None, new: dict[str, Any]) -> bool:
    """Return True if new differs from old only by a small position change."""
    if old is None:
        return False
    keys = set(old) | set(new)
    keys -= {"position", "position_updated_at"}
    if any(old.get(key) != new.get(key) for key in keys):
        return False
    old_pos, new_pos = old.get("position"), new.get("position")
    if old_pos is None or new_pos is None:
        return old_pos == new_pos
    return abs(old_pos - new_pos) < POSITION_STEP


def resume_position(session: dict[str, Any]) -> float:
    """Return the position to resume the session's video from.

    A session still playing has moved on since its last event.
    """
    if session.get("stopped_at") is None:
        return float(_position_at(session, dt_util.utcnow()) or 0)
    return float(session.get("position") or 0)
