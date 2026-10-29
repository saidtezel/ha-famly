"""Diagnostics for Famly: sync state and the *shape* of the feed, no content."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import FamlyConfigEntry
from .api import FamlyError
from .const import CONF_ACCESS_TOKEN, CONF_DEVICE_ID
from .photo_sync import feed_photos

TO_REDACT = {CONF_EMAIL, CONF_PASSWORD, CONF_ACCESS_TOKEN, CONF_DEVICE_ID}


def _pattern(value: Any) -> Any:
    """Show the format of a value without its content: digits -> 9, letters -> a."""
    if isinstance(value, str):
        return re.sub(r"[A-Za-z]", "a", re.sub(r"\d", "9", value))[:40]
    return type(value).__name__


def _image_shape(img: Any) -> dict[str, Any]:
    if not isinstance(img, dict):
        return {"type": type(img).__name__}
    url = img.get("url_big") or img.get("url") or ""
    created = img.get("createdAt")
    return {
        "keys": sorted(img),
        "createdAt": (
            {k: _pattern(v) for k, v in created.items()}
            if isinstance(created, dict)
            else _pattern(created)
        ),
        "url_host": urlsplit(url).hostname if isinstance(url, str) else None,
        "url_has_size_segment": bool(re.search(r"/\d+x\d+/", str(url))),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: FamlyConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    sync = coordinator.photo_sync
    out: dict[str, Any] = {
        "entry": {"data": async_redact_data(dict(entry.data), TO_REDACT), "options": dict(entry.options)},
        "children": len(coordinator.children),
    }
    if sync:
        out["photo_sync"] = {
            "enabled": sync.enabled,
            "remembered_uploads": len(sync.uploaded),
            "total_uploaded": sync.total_uploaded,
            "synced_up_to": sync.newest_synced.isoformat() if sync.newest_synced else None,
            "import_done_for": sync.backfill_done_for,
            "last_run": sync.last_run.isoformat() if sync.last_run else None,
            "last_scan": sync.stats,
            "pending": sync.pending,
            "last_error": sync.last_error,
        }
    try:
        items = await coordinator.client.get_feed(first=10)
    except FamlyError as err:
        out["feed_probe"] = {"error": str(err)}
        return out
    out["feed_probe"] = {
        "items": len(items),
        "sample": [
            {
                "keys": sorted(item),
                "originatorId_prefix": str(item.get("originatorId") or "").split(":")[0],
                "systemPostTypeClass": item.get("systemPostTypeClass") or None,
                "createdDate": _pattern(item.get("createdDate")),
                "images": len(item.get("images") or []),
                "first_image": _image_shape((item.get("images") or [None])[0])
                if item.get("images")
                else None,
                "photos_recognised": len(feed_photos(item)),
            }
            for item in items
        ],
    }
    return out
