"""Hourly sync of nursery news-feed photos to Immich.

Photos are downloaded one at a time into a scratch folder inside Home
Assistant's media directory (Immich's upload action only accepts local media),
given the post's date in EXIF and file mtime (Famly strips all metadata), handed
to `immich.upload_file`, and deleted straight afterwards. Nothing is kept
locally; uploaded image ids are remembered so nothing is sent twice.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
import struct
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import piexif
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import FamlyAuthError, FamlyClient, FamlyError
from .const import CONF_BACKFILL_SINCE, CONF_IMMICH_ALBUM, CONF_IMMICH_ENTRY, DOMAIN
from .parser import parse_dt

if TYPE_CHECKING:
    from . import FamlyConfigEntry

_LOGGER = logging.getLogger(__name__)

SYNC_INTERVAL = timedelta(hours=1)
FIRST_RUN_DELAY = 120  # seconds after setup
FEED_PAGE_SIZE = 20
MAX_PAGES_PER_RUN = 50
MAX_UPLOADS_PER_RUN = 60
OVERLAP = timedelta(days=2)  # re-check recent posts in case photos were added late
PAUSE_SECONDS = 1.0
MAX_REMEMBERED = 20000
UPLOAD_DIR = "famly_immich_upload"
STORAGE_VERSION = 1
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"}


@dataclass(slots=True, frozen=True)
class FeedPhoto:
    """One photo attached to a feed post."""

    image_id: str
    url: str
    posted: datetime


# ---------------------------------------------------------------- parsing


def feed_photos(item: dict[str, Any]) -> list[FeedPhoto]:
    """Photos attached to one feed item. Only real posts, not system items."""
    if not str(item.get("originatorId") or "").startswith("Post:"):
        return []
    posted = parse_dt(item.get("createdDate"))
    photos: list[FeedPhoto] = []
    for img in item.get("images") or []:
        if not isinstance(img, dict):
            continue
        image_id = img.get("imageId") or img.get("id")
        url = _image_url(img)
        taken = posted or parse_dt(img.get("createdAt"))
        if image_id and url and taken:
            photos.append(FeedPhoto(str(image_id), url, taken))
    return photos


def _image_url(img: dict[str, Any]) -> str | None:
    secret = img.get("secret")
    if isinstance(secret, dict) and secret.get("prefix") and secret.get("key"):
        url = f"{secret['prefix']}/{secret['key']}"
        if secret.get("path"):
            url += f"/{secret['path']}"
        if secret.get("expires"):
            url += f"?expires={secret['expires']}"
        return url
    if img.get("prefix") and img.get("key"):
        # Original resolution, as used by famly-fetch.
        return f"{img['prefix']}/{img['key']}"
    big = img.get("big")
    if isinstance(big, dict) and big.get("url"):
        return str(big["url"])
    return img.get("url_big") or img.get("url")


def clean_album_id(value: str | None) -> str | None:
    """Accept either an album id or a pasted Immich album URL."""
    if not value:
        return None
    value = value.strip().rstrip("/")
    match = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value, re.IGNORECASE
    )
    return match.group(0) if match else value.rsplit("/", 1)[-1] or None


def write_photo_file(path: Path, data: bytes, taken: datetime) -> None:
    """Write the photo and stamp the date Famly stripped (EXIF + mtime)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    stamp = taken.strftime("%Y:%m:%d %H:%M:%S").encode()
    offset = taken.strftime("%z")
    offset = f"{offset[:3]}:{offset[3:]}".encode() if len(offset) == 5 else b""
    exif: dict[str, Any] = {
        "0th": {piexif.ImageIFD.DateTime: stamp},
        "Exif": {
            piexif.ExifIFD.DateTimeOriginal: stamp,
            piexif.ExifIFD.DateTimeDigitized: stamp,
        },
    }
    if offset:
        exif["Exif"][piexif.ExifIFD.OffsetTimeOriginal] = offset
        exif["Exif"][piexif.ExifIFD.OffsetTimeDigitized] = offset
    try:
        piexif.insert(piexif.dump(exif), str(path))
    except (piexif.InvalidImageDataError, ValueError, struct.error):
        _LOGGER.debug("Not a JPEG, leaving %s without EXIF date", path.name)
    ts = taken.timestamp()
    os.utime(path, (ts, ts))


# ------------------------------------------------------------------- sync


class PhotoSync:
    """Pushes new news-feed photos to Immich once an hour."""

    def __init__(
        self, hass: HomeAssistant, entry: FamlyConfigEntry, client: FamlyClient
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.photo_sync.{entry.entry_id}"
        )
        self._lock = asyncio.Lock()
        self._listeners: list[CALLBACK_TYPE] = []
        self.uploaded: dict[str, str] = {}
        self.total_uploaded = 0
        self.newest_synced: datetime | None = None
        self.backfill_done_for: str | None = None
        self.last_run: datetime | None = None
        self.last_uploaded = 0
        self.pending = 0
        self.last_error: str | None = None

    # ------------------------------------------------------------ settings

    @property
    def immich_entry_id(self) -> str | None:
        return self.entry.options.get(CONF_IMMICH_ENTRY) or None

    @property
    def album_id(self) -> str | None:
        return clean_album_id(self.entry.options.get(CONF_IMMICH_ALBUM))

    @property
    def backfill_since(self) -> str | None:
        value = self.entry.options.get(CONF_BACKFILL_SINCE)
        return str(value) if value else None

    @property
    def enabled(self) -> bool:
        return self.immich_entry_id is not None

    # ------------------------------------------------------------ lifecycle

    async def async_start(self) -> None:
        stored = await self._store.async_load() or {}
        self.uploaded = dict(stored.get("uploaded") or {})
        self.total_uploaded = int(stored.get("total_uploaded") or len(self.uploaded))
        self.newest_synced = parse_dt(stored.get("newest_synced"))
        self.backfill_done_for = stored.get("backfill_done_for")
        self.last_run = parse_dt(stored.get("last_run"))
        self.last_error = stored.get("last_error")
        self.pending = int(stored.get("pending") or 0)
        await self.hass.async_add_executor_job(self._clean_upload_dir)

        if not self.enabled:
            return

        @callback
        def _scheduled(_now: datetime) -> None:
            self.entry.async_create_background_task(
                self.hass, self.async_sync(), f"{DOMAIN} photo sync"
            )

        self.entry.async_on_unload(
            async_track_time_interval(self.hass, _scheduled, SYNC_INTERVAL)
        )
        self.entry.async_on_unload(
            async_call_later(self.hass, FIRST_RUN_DELAY, _scheduled)
        )

    @callback
    def async_add_listener(self, update: CALLBACK_TYPE) -> Callable[[], None]:
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    @callback
    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    async def _save(self) -> None:
        if len(self.uploaded) > MAX_REMEMBERED:
            newest = sorted(self.uploaded.items(), key=lambda kv: kv[1])[-MAX_REMEMBERED:]
            self.uploaded = dict(newest)
        await self._store.async_save(
            {
                "uploaded": self.uploaded,
                "total_uploaded": self.total_uploaded,
                "newest_synced": self.newest_synced.isoformat() if self.newest_synced else None,
                "backfill_done_for": self.backfill_done_for,
                "last_run": self.last_run.isoformat() if self.last_run else None,
                "last_error": self.last_error,
                "pending": self.pending,
            }
        )

    # ------------------------------------------------------------ the run

    async def async_sync(self) -> int:
        """Run one sync. Returns how many photos were uploaded."""
        if self._lock.locked():
            _LOGGER.debug("Photo sync already running")
            return 0
        async with self._lock:
            try:
                uploaded = await self._sync()
            except FamlyAuthError as err:
                self.last_error = f"Famly login failed: {err}"
                self.entry.async_start_reauth(self.hass)
                uploaded = 0
            except (FamlyError, HomeAssistantError, OSError) as err:
                self.last_error = str(err)
                _LOGGER.warning("Famly photo sync failed: %s", err)
                uploaded = 0
            self.last_run = dt_util.utcnow()
            self.last_uploaded = uploaded
            await self._save()
            self._notify()
            return uploaded

    async def _sync(self) -> int:
        if not self.enabled:
            return 0
        immich_entry = self.hass.config_entries.async_get_entry(self.immich_entry_id or "")
        if immich_entry is None or immich_entry.domain != "immich":
            raise HomeAssistantError("The selected Immich integration no longer exists")
        if immich_entry.state is not ConfigEntryState.LOADED:
            raise HomeAssistantError("The Immich integration is not loaded")

        now = dt_util.utcnow()
        since = self.backfill_since
        backfill_pending = bool(since) and self.backfill_done_for != since

        if self.newest_synced is None and not backfill_pending:
            # First run without a backfill: start from now, don't import history.
            self.newest_synced = now
            self.last_error = None
            return 0

        stops: list[datetime] = []
        if self.newest_synced is not None:
            stops.append(self.newest_synced - OVERLAP)
        if backfill_pending and since:
            start = dt_util.start_of_local_day(date.fromisoformat(since[:10]))
            stops.append(start)
        stop_at = min(stops)

        photos, complete = await self._collect(stop_at)
        todo = sorted(
            {p.image_id: p for p in photos if p.image_id not in self.uploaded}.values(),
            key=lambda p: p.posted,
        )
        uploaded = 0
        errors: list[str] = []
        for photo in todo[:MAX_UPLOADS_PER_RUN]:
            try:
                await self._upload(photo, immich_entry.entry_id)
            except FamlyAuthError:
                raise
            except (FamlyError, HomeAssistantError, OSError) as err:
                errors.append(f"{photo.image_id}: {err}")
                _LOGGER.warning("Could not upload Famly photo %s: %s", photo.image_id, err)
                if len(errors) >= 5:
                    break
                continue
            self.uploaded[photo.image_id] = dt_util.utcnow().isoformat()
            self.total_uploaded += 1
            uploaded += 1
            if uploaded % 10 == 0:
                await self._save()
            await asyncio.sleep(PAUSE_SECONDS)

        self.pending = len(todo) - uploaded
        if self.pending == 0 and complete:
            newest = max((p.posted for p in photos), default=None)
            if newest and (self.newest_synced is None or newest > self.newest_synced):
                self.newest_synced = newest
            elif self.newest_synced is None:
                self.newest_synced = now
            if backfill_pending:
                self.backfill_done_for = since
        self.last_error = "; ".join(errors[:3]) if errors else None
        if uploaded:
            _LOGGER.info("Uploaded %s Famly photo(s) to Immich", uploaded)
        return uploaded

    async def _collect(self, stop_at: datetime) -> tuple[list[FeedPhoto], bool]:
        """Walk the feed back to `stop_at`. Returns (photos, reached stop_at)."""
        photos: list[FeedPhoto] = []
        cursor: str | None = None
        older_than: str | None = None
        for page in range(MAX_PAGES_PER_RUN):
            if page:
                await asyncio.sleep(PAUSE_SECONDS)
            items = await self.client.get_feed(cursor, older_than, FEED_PAGE_SIZE)
            if not items:
                return photos, True
            for item in items:
                posted = parse_dt(item.get("createdDate"))
                if posted is None or posted >= stop_at:
                    photos.extend(feed_photos(item))
            last = items[-1]
            last_posted = parse_dt(last.get("createdDate"))
            # Stop on the page's last item, not the first old one: pinned
            # posts can appear out of order at the top of the feed.
            if last_posted is not None and last_posted < stop_at:
                return photos, True
            cursor = last.get("feedItemId")
            older_than = last.get("createdDate")
            if not cursor and not older_than:
                return photos, True
        return photos, False

    async def _upload(self, photo: FeedPhoto, immich_entry_id: str) -> None:
        data, content_type = await self.client.download(photo.url)
        ext = mimetypes.guess_extension((content_type or "").split(";")[0].strip()) or ".jpg"
        if ext not in ALLOWED_EXTENSIONS:
            ext = ".jpg"
        taken = dt_util.as_local(photo.posted)
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "", photo.image_id)[:12] or "photo"
        filename = f"famly_{taken:%Y%m%d_%H%M%S}_{safe_id}{ext}"
        source_dir_id, base = self._media_dir()
        path = Path(base, UPLOAD_DIR, filename)

        await self.hass.async_add_executor_job(write_photo_file, path, data, taken)
        try:
            service_data: dict[str, Any] = {
                "config_entry_id": immich_entry_id,
                "file": {
                    "media_content_id": (
                        f"media-source://media_source/{source_dir_id}/{UPLOAD_DIR}/{filename}"
                    ),
                    "media_content_type": mimetypes.guess_type(filename)[0] or "image/jpeg",
                },
            }
            if self.album_id:
                service_data["album_id"] = self.album_id
            await self.hass.services.async_call(
                "immich", "upload_file", service_data, blocking=True
            )
        finally:
            await self.hass.async_add_executor_job(_unlink, path)

    def _media_dir(self) -> tuple[str, str]:
        dirs = self.hass.config.media_dirs
        if not dirs:
            raise HomeAssistantError("Home Assistant has no media folder configured")
        key = "local" if "local" in dirs else next(iter(dirs))
        return key, dirs[key]

    def _clean_upload_dir(self) -> None:
        """Remove anything left behind by an interrupted run."""
        dirs = self.hass.config.media_dirs
        for base in dirs.values():
            folder = Path(base, UPLOAD_DIR)
            if not folder.is_dir():
                continue
            for leftover in folder.iterdir():
                if leftover.is_file() and leftover.name.startswith("famly_"):
                    _unlink(leftover)


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as err:
        _LOGGER.warning("Could not delete temporary file %s: %s", path, err)
