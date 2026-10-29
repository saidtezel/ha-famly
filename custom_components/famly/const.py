"""Constants for the Famly integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "famly"

CONF_REGION: Final = "region"
CONF_DEVICE_ID: Final = "device_id"
CONF_ACCESS_TOKEN: Final = "access_token"
CONF_IMMICH_ENTRY: Final = "immich_entry_id"
CONF_IMMICH_ALBUM: Final = "immich_album_id"
CONF_BACKFILL_SINCE: Final = "backfill_since"

REGIONS: Final = {
    "famly.co": "https://app.famly.co",
    "famly.de": "https://app.famly.de",
}
DEFAULT_REGION: Final = "famly.co"

# Polling. Famly has no push channel for third parties, so we poll gently:
# often while the nursery is open, rarely otherwise.
ACTIVE_INTERVAL: Final = timedelta(minutes=2)
IDLE_INTERVAL: Final = timedelta(minutes=30)
OPENING_MARGIN: Final = timedelta(minutes=45)

# Slower-changing data is refreshed on its own cadence inside the coordinator.
PHOTO_INTERVAL: Final = timedelta(minutes=15)
UPCOMING_INTERVAL: Final = timedelta(hours=1)
OPENING_HOURS_INTERVAL: Final = timedelta(hours=12)
UPCOMING_DAYS: Final = 30

EVENT_CHECK_IN: Final = "check_in"
EVENT_CHECK_OUT: Final = "check_out"
EVENT_ROOM_CHANGE: Final = "room_change"
EVENT_NAP_START: Final = "nap_start"
EVENT_NAP_END: Final = "nap_end"
EVENT_NAPPY: Final = "nappy_change"
EVENT_MEAL: Final = "meal"
EVENT_ACTIVITY: Final = "activity"
EVENT_SICK: Final = "sick"

EVENT_TYPES: Final = [
    EVENT_CHECK_IN,
    EVENT_CHECK_OUT,
    EVENT_ROOM_CHANGE,
    EVENT_NAP_START,
    EVENT_NAP_END,
    EVENT_NAPPY,
    EVENT_MEAL,
    EVENT_ACTIVITY,
    EVENT_SICK,
]

STATUS_AT_NURSERY: Final = "at_nursery"
STATUS_PICKED_UP: Final = "picked_up"
STATUS_NOT_ARRIVED: Final = "not_arrived"
STATUS_SICK: Final = "sick"
STATUS_ABSENT: Final = "absent"
STATUS_HOLIDAY: Final = "holiday"
STATUS_OPTIONS: Final = [
    STATUS_AT_NURSERY,
    STATUS_PICKED_UP,
    STATUS_NOT_ARRIVED,
    STATUS_SICK,
    STATUS_ABSENT,
    STATUS_HOLIDAY,
]
