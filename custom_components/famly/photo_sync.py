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
from PIL import Image

from .api import (
    FamlyAuthError,
    FamlyClient,
    FamlyConnectionError,
    FamlyError,
    FamlyLinkExpiredError,
    FamlyRateLimitError,
)
from .const import CONF_BACKFILL_SINCE, CONF_IMMICH_ALBUM, CONF_IMMICH_ENTRY, DOMAIN
from .parser import parse_dt

if TYPE_CHECKING:
    from . import FamlyConfigEntry

_LOGGER = logging.getLogger(__name__)

SYNC_INTERVAL = timedelta(hours=1)
FIRST_RUN_DELAY = 120  # seconds after setup
FEED_PAGE_SIZE = 10  # Famly treats "first" as advisory
MAX_FEED_PAGES = 5000  # safety stop for a runaway walk (~50,000 posts)
OVERLAP = timedelta(days=2)  # re-check recent posts in case photos were added late

# Pacing, modelled on famly-fetch: short gap between photos, longer pause
# between batches. A run keeps going in batches until nothing is left.
PAUSE_SECONDS = 1.0
BATCH_SIZE = 20
BATCH_PAUSE_SECONDS = 10.0

# Back-off when Famly or Immich push back (429, 5xx, dropped connections).
BACKOFF_START_SECONDS = 30.0
BACKOFF_MAX_SECONDS = 900.0
MAX_CONSECUTIVE_FAILURES = 6  # then stop and resume at the next hourly run
MAX_FEED_RETRIES = 5
MAX_REWALKS = 5  # fresh feed reads per run when signed links expire
REWALK_AFTER = timedelta(hours=6)  # links last about a day; refresh well before

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
    """Photos attached to one feed item.

    Automated posts (sign-in notices and the like) carry systemPostTypeClass
    and are skipped; everything a person posted is included.
    """
    if item.get("systemPostTypeClass"):
        return []
    post_date = parse_dt(item.get("createdDate"))
    photos: list[FeedPhoto] = []
    for img in item.get("images") or []:
        if not isinstance(img, dict):
            continue
        image_id = img.get("imageId") or img.get("id")
        url = _image_url(img)
        taken = _image_time(img) or post_date
        if image_id and url and taken:
            photos.append(FeedPhoto(str(image_id), url, taken))
    return photos


def _image_time(img: dict[str, Any]) -> datetime | None:
    """Image createdAt is an object: {"date": "...", "timezone": "Europe/London"}."""
    created = img.get("createdAt")
    if isinstance(created, dict):
        return parse_dt(created.get("date"))
    return parse_dt(created)


_SIZE_SEGMENT = re.compile(r"/(\d+)x(\d+)/")


def _image_url(img: dict[str, Any]) -> str | None:
    """Highest-resolution URL for a feed image.

    Famly's CDN URLs carry the served size as a "/WxH/" path segment and will
    serve any size up to the original, so ask for the original dimensions.
    """
    big = img.get("big")
    candidate = (
        img.get("url_big")
        or (big.get("url") if isinstance(big, dict) else None)
        or img.get("url")
    )
    if not candidate:
        secret = img.get("secret")
        if isinstance(secret, dict) and secret.get("prefix") and secret.get("key"):
            candidate = f"{secret['prefix']}/{secret['key']}"
            if secret.get("path"):
                candidate += f"/{secret['path']}"
            if secret.get("expires"):
                candidate += f"?expires={secret['expires']}"
        elif img.get("prefix") and img.get("key"):
            candidate = f"{img['prefix']}/{img['key']}"
        else:
            return None
    width, height = img.get("width"), img.get("height")
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        return _SIZE_SEGMENT.sub(f"/{width}x{height}/", str(candidate), count=1)
    return str(candidate)


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
    check_image(path)
    ts = taken.timestamp()
    os.utime(path, (ts, ts))


class BrokenImageError(ValueError):
    """The downloaded file is not a complete, decodable image."""


def check_image(path: Path) -> None:
    """Fully decode the image so a truncated download never reaches Immich."""
    try:
        with Image.open(path) as img:
            img.load()
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as err:
        raise BrokenImageError(f"Downloaded image is incomplete or corrupt: {err}") from err


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
            hass, STORAGE_VERSION, f"{DOMAIN}.photo_sync_v2.{entry.entry_id}"
        )
        # 0.2.0/0.2.1 uploaded truncated photos; forget that state so they
        # are uploaded again, complete.
        self._old_store: Store[dict[str, Any]] = Store(
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
        self.stats: dict[str, int] = {}
        self.status = "idle"
        self.progress_done = 0
        self.progress_total = 0
        self._task: asyncio.Task | None = None

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
        await self._old_store.async_remove()
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
            self.async_request_sync()

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

    @callback
    def async_request_sync(self) -> None:
        """Start a sync in the background unless one is already running."""
        if self._task and not self._task.done():
            _LOGGER.debug("Photo sync already running")
            return
        self._task = self.entry.async_create_background_task(
            self.hass, self.async_sync(), f"{DOMAIN} photo sync"
        )

    async def async_sync(self) -> int:
        """Run one sync until caught up. Returns how many photos were uploaded."""
        if self._lock.locked():
            _LOGGER.debug("Photo sync already running")
            return 0
        async with self._lock:
            uploaded = 0
            try:
                uploaded = await self._sync()
            except FamlyAuthError as err:
                self.last_error = f"Famly login failed: {err}"
                self.entry.async_start_reauth(self.hass)
            except (FamlyError, HomeAssistantError, OSError) as err:
                self.last_error = str(err)
                _LOGGER.warning("Famly photo sync failed: %s", err)
            finally:
                self.status = "idle"
                self.last_run = dt_util.utcnow()
                self.last_uploaded = uploaded
                await self._save()
                self._notify()
            return uploaded

    def _set_status(self, status: str) -> None:
        self.status = status
        self._notify()

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
            # First run without an import: start from now, don't import history.
            self.newest_synced = now
            self.last_error = None
            return 0

        stops: list[datetime] = []
        if self.newest_synced is not None:
            stops.append(self.newest_synced - OVERLAP)
        if backfill_pending and since:
            stops.append(dt_util.start_of_local_day(date.fromisoformat(since[:10])))
        stop_at = min(stops)

        total_uploaded = 0
        errors: list[str] = []
        outcome = "stopped"
        photos: list[FeedPhoto] = []
        complete = False
        for _walk in range(MAX_REWALKS):
            self._set_status("scanning")
            walked_at = dt_util.utcnow()
            photos, complete = await self._collect(stop_at)
            todo = sorted(
                {p.image_id: p for p in photos if p.image_id not in self.uploaded}.values(),
                key=lambda p: p.posted,
            )
            self.progress_done, self.progress_total = 0, len(todo)
            self._log_scan(len(photos), len(todo))

            uploaded, outcome = await self._upload_all(
                todo, immich_entry.entry_id, walked_at, errors
            )
            total_uploaded += uploaded
            self.pending = sum(1 for p in todo if p.image_id not in self.uploaded)
            if outcome != "rewalk":
                break
            _LOGGER.info("Famly photo links expired; reading the feed again")

        caught_up = (
            outcome == "done"
            and self.pending == 0
            and complete
            and self.stats.get("feed_items", 0) > 0
        )
        if caught_up:
            newest = max((p.posted for p in photos), default=None)
            if newest and (self.newest_synced is None or newest > self.newest_synced):
                self.newest_synced = newest
            if backfill_pending:
                self.backfill_done_for = since
                _LOGGER.info("Famly photo import since %s is complete", since)

        self.last_error = "; ".join(e.removeprefix("skip:") for e in errors[-3:]) or None
        if total_uploaded:
            _LOGGER.info("Uploaded %s Famly photo(s) to Immich", total_uploaded)
        return total_uploaded

    async def _upload_all(
        self,
        todo: list[FeedPhoto],
        immich_entry_id: str,
        walked_at: datetime,
        errors: list[str],
    ) -> tuple[int, str]:
        """Upload in paced batches with back-off.

        Returns (uploaded, outcome) where outcome is "done", "rewalk" (links
        expired, read the feed again) or "stopped" (too many failures in a
        row; the next hourly run resumes).
        """
        uploaded = 0
        consecutive = 0
        backoff = BACKOFF_START_SECONDS
        index = 0
        self._set_status("uploading")
        while index < len(todo):
            photo = todo[index]
            if photo.image_id in self.uploaded:
                index += 1
                continue
            if dt_util.utcnow() - walked_at > REWALK_AFTER:
                return uploaded, "rewalk"
            try:
                await self._upload(photo, immich_entry_id)
            except FamlyAuthError:
                raise
            except FamlyLinkExpiredError:
                return uploaded, "rewalk"
            except (FamlyConnectionError, HomeAssistantError) as err:
                # Transient: rate limit, server error, dropped connection,
                # truncated download, Immich unavailable. Wait, retry same photo.
                consecutive += 1
                retry_after = err.retry_after if isinstance(err, FamlyRateLimitError) else None
                if consecutive >= MAX_CONSECUTIVE_FAILURES:
                    errors.append(f"{photo.image_id}: {err}")
                    _LOGGER.warning(
                        "Famly photo sync stopping after %s failures in a row (%s); "
                        "it will resume at the next hourly run",
                        consecutive,
                        err,
                    )
                    return uploaded, "stopped"
                delay = min(retry_after if retry_after is not None else backoff, BACKOFF_MAX_SECONDS)
                backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)
                _LOGGER.info(
                    "Famly photo upload failed (%s); waiting %.0fs before retrying", err, delay
                )
                self._set_status("waiting")
                await _sleep(delay)
                self._set_status("uploading")
                continue
            except (BrokenImageError, FamlyError, OSError) as err:
                # This photo is the problem, not the connection: skip it this run.
                errors.append(f"skip:{photo.image_id}: {err}")
                _LOGGER.warning("Skipping Famly photo %s: %s", photo.image_id, err)
                index += 1
                continue

            self.uploaded[photo.image_id] = dt_util.utcnow().isoformat()
            self.total_uploaded += 1
            uploaded += 1
            self.progress_done += 1
            consecutive = 0
            backoff = BACKOFF_START_SECONDS
            index += 1
            if uploaded % BATCH_SIZE == 0:
                await self._save()
                self._notify()
                _LOGGER.info(
                    "Famly photo sync: %s of %s uploaded", self.progress_done, self.progress_total
                )
                if index < len(todo):
                    await _sleep(BATCH_PAUSE_SECONDS)
            elif index < len(todo):
                await _sleep(PAUSE_SECONDS)
        return uploaded, "done"

    def _log_scan(self, in_range: int, new: int) -> None:
        _LOGGER.info(
            "Famly photo sync: %s feed item(s) on %s page(s), %s with images, "
            "%s photo(s) in range, %s new",
            self.stats.get("feed_items", 0),
            self.stats.get("pages", 0),
            self.stats.get("items_with_images", 0),
            in_range,
            new,
        )
        if self.stats.get("items_with_images") and not in_range:
            _LOGGER.warning(
                "Famly feed has posts with images but none could be read; "
                "the feed format may have changed. Download diagnostics for details"
            )

    async def _get_feed_page(
        self, cursor: str | None, older_than: str | None
    ) -> list[dict[str, Any]]:
        """One feed page, retrying with back-off when Famly pushes back."""
        backoff = BACKOFF_START_SECONDS
        for attempt in range(MAX_FEED_RETRIES):
            try:
                return await self.client.get_feed(cursor, older_than, FEED_PAGE_SIZE)
            except FamlyConnectionError as err:
                if attempt == MAX_FEED_RETRIES - 1:
                    raise
                retry_after = err.retry_after if isinstance(err, FamlyRateLimitError) else None
                delay = min(retry_after if retry_after is not None else backoff, BACKOFF_MAX_SECONDS)
                backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)
                _LOGGER.info("Reading the Famly feed failed (%s); retrying in %.0fs", err, delay)
                await _sleep(delay)
        return []

    async def _collect(self, stop_at: datetime) -> tuple[list[FeedPhoto], bool]:
        """Walk the feed back to `stop_at`. Returns (photos, reached stop_at)."""
        photos: list[FeedPhoto] = []
        cursor: str | None = None
        older_than: str | None = None
        self.stats = {"pages": 0, "feed_items": 0, "items_with_images": 0, "photos": 0}
        for page in range(MAX_FEED_PAGES):
            if page:
                await _sleep(PAUSE_SECONDS)
            items = await self._get_feed_page(cursor, older_than)
            self.stats["pages"] += 1
            if not items:
                return photos, True
            for item in items:
                self.stats["feed_items"] += 1
                if item.get("images"):
                    self.stats["items_with_images"] += 1
                posted = parse_dt(item.get("createdDate"))
                if posted is None or posted >= stop_at:
                    found = feed_photos(item)
                    self.stats["photos"] += len(found)
                    photos.extend(found)
            last = items[-1]
            last_posted = parse_dt(last.get("createdDate"))
            # Stop on the page's last item, not the first old one: pinned
            # posts can appear out of order at the top of the feed.
            if last_posted is not None and last_posted < stop_at:
                return photos, True
            next_cursor = last.get("feedItemId")
            if not next_cursor or next_cursor == cursor or last_posted is None:
                _LOGGER.warning(
                    "Famly feed paging stopped: cursor did not advance or post has no date"
                )
                return photos, True
            cursor = next_cursor
            older_than = last_posted.isoformat().replace("+00:00", "Z")
            if page % 20 == 19:
                self._notify()
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

        try:
            await self.hass.async_add_executor_job(write_photo_file, path, data, taken)
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


async def _sleep(seconds: float) -> None:
    """Indirection so tests can skip real waiting."""
    if seconds > 0:
        await asyncio.sleep(seconds)


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as err:
        _LOGGER.warning("Could not delete temporary file %s: %s", path, err)
