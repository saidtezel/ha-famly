"""Tests for the Immich photo sync."""

from __future__ import annotations

import io
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import piexif
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from PIL import Image
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.famly.const import (
    CONF_BACKFILL_SINCE,
    CONF_IMMICH_ALBUM,
    CONF_IMMICH_ENTRY,
)
from custom_components.famly.photo_sync import (
    UPLOAD_DIR,
    clean_album_id,
    feed_photos,
    write_photo_file,
)

NOW = "2026-10-28T16:00:00+00:00"
ALBUM = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (90, 30, 160)).save(buf, "JPEG")
    return buf.getvalue()


def _famly_time(iso: str) -> str:
    """Famly's REST format: '2026-10-28 14:05:09.000000', UTC, no zone."""
    return datetime.fromisoformat(iso).astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.000000")


def _post(post_id: str, created: str, *image_ids: str, system: str | None = None) -> dict:
    item = {
        "feedItemId": f"fi-{post_id}",
        "originatorId": f"Post:{post_id}",
        "createdDate": _famly_time(created),
        "body": "Today we painted",
        "images": [
            {
                "imageId": iid,
                "url": f"https://img.famly.example/image/abc/600x450/{iid}.jpg?expires=1",
                "url_big": f"https://img.famly.example/image/abc/1024x768/{iid}.jpg?expires=1",
                "width": 4032,
                "height": 3024,
                "createdAt": {"date": _famly_time(created), "timezone": "UTC"},
            }
            for iid in image_ids
        ],
        "videos": [],
    }
    if system:
        item["systemPostTypeClass"] = system
    return item


# ------------------------------------------------------------- pure helpers


def test_feed_photos_real_format():
    photos = feed_photos(_post("p1", "2026-10-28T13:05:00+00:00", "i1", "i2"))
    assert [p.image_id for p in photos] == ["i1", "i2"]
    # Full resolution: size segment rewritten to the original dimensions.
    assert photos[0].url == "https://img.famly.example/image/abc/4032x3024/i1.jpg?expires=1"
    # Zone-less Famly timestamp is read as UTC.
    assert photos[0].posted == datetime.fromisoformat("2026-10-28T13:05:00+00:00")


def test_feed_photos_skips_system_posts_only():
    assert feed_photos(_post("s", "2026-10-28T13:05:00+00:00", "x", system="CheckinPost")) == []
    item = _post("p", "2026-10-28T13:05:00+00:00", "y")
    item["originatorId"] = "SomethingElse:1"
    assert len(feed_photos(item)) == 1


def test_feed_photos_prefers_image_time():
    item = _post("p", "2026-10-28T15:00:00+00:00", "y")
    item["images"][0]["createdAt"] = {"date": "2026-10-28 14:59:00.000000", "timezone": "UTC"}
    assert feed_photos(item)[0].posted == datetime.fromisoformat("2026-10-28T14:59:00+00:00")


def test_feed_photos_secret_image():
    item = {
        "originatorId": "Post:9",
        "createdDate": "2026-10-28 13:05:00.000000",
        "images": [
            {
                "id": "s1",
                "secret": {"prefix": "https://p", "key": "k", "path": "a.jpg", "expires": "123"},
            }
        ],
    }
    assert feed_photos(item)[0].url == "https://p/k/a.jpg?expires=123"


@pytest.mark.parametrize(
    "value",
    [ALBUM, f"https://photos.home.lan/albums/{ALBUM}", f"https://photos.home.lan/albums/{ALBUM}/"],
)
def test_clean_album_id(value):
    assert clean_album_id(value) == ALBUM


def test_write_photo_file_stamps_date(tmp_path: Path):
    taken = datetime.fromisoformat("2026-10-28T14:05:09+00:00")
    path = tmp_path / "x.jpg"
    write_photo_file(path, _jpeg(), taken)
    exif = piexif.load(str(path))
    assert exif["Exif"][piexif.ExifIFD.DateTimeOriginal] == b"2026:10:28 14:05:09"
    assert exif["Exif"][piexif.ExifIFD.OffsetTimeOriginal] == b"+00:00"
    assert os.path.getmtime(path) == taken.timestamp()
    Image.open(path).verify()  # still a valid image


def test_write_photo_file_non_jpeg(tmp_path: Path):
    path = tmp_path / "x.png"
    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, "PNG")
    taken = datetime.fromisoformat("2026-10-28T14:05:09+00:00")
    write_photo_file(path, buf.getvalue(), taken)
    assert os.path.getmtime(path) == taken.timestamp()


# ------------------------------------------------------------ in Home Assistant


@pytest.fixture
async def media_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    await hass.config.async_set_time_zone("Europe/London")
    hass.config.media_dirs = {"local": str(tmp_path)}
    return tmp_path


@pytest.fixture
def immich_entry(hass: HomeAssistant):
    entry = MockConfigEntry(domain="immich", title="Immich", state=ConfigEntryState.LOADED)
    entry.add_to_hass(hass)
    yield entry
    # Stand-in only: stop the harness from unloading the real Immich component.
    entry.mock_state(hass, ConfigEntryState.NOT_LOADED)


@pytest.fixture
def uploads(hass: HomeAssistant, media_dir: Path):
    """Fake immich.upload_file that checks the file it is handed."""
    calls: list[dict] = []

    async def _upload(call: ServiceCall) -> None:
        media_id = call.data["file"]["media_content_id"]
        rel = media_id.removeprefix("media-source://media_source/local/")
        path = media_dir / rel
        assert path.is_file(), "file must exist while Immich reads it"
        exif = piexif.load(str(path))
        calls.append(
            {
                "name": path.name,
                "album": call.data.get("album_id"),
                "taken": exif["Exif"][piexif.ExifIFD.DateTimeOriginal].decode(),
                "type": call.data["file"]["media_content_type"],
            }
        )

    hass.services.async_register("immich", "upload_file", _upload)
    return calls


@pytest.fixture
def no_pause():
    """Record waits instead of sleeping."""
    waits: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    with patch("custom_components.famly.photo_sync._sleep", _fake_sleep):
        yield waits


async def _setup(hass, config_entry, mock_client, immich_entry, **options):
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        options={CONF_IMMICH_ENTRY: immich_entry.entry_id, **options},
    )
    mock_client.download = AsyncMock(return_value=(_jpeg(), "image/jpeg"))
    mock_client.get_feed = AsyncMock(return_value=[])
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry.runtime_data.photo_sync


@pytest.mark.freeze_time(NOW)
async def test_sync_uploads_new_photos_once_and_deletes_them(
    hass, mock_client, config_entry, immich_entry, uploads, media_dir, no_pause
):
    sync = await _setup(
        hass, config_entry, mock_client, immich_entry,
        **{CONF_IMMICH_ALBUM: f"https://immich.lan/albums/{ALBUM}"},
    )
    assert hass.states.get("button.famly_pat_parent_sync_photos_to_immich")

    # First run without an import date only sets the starting point.
    mock_client.get_feed.return_value = [_post("old", "2026-10-20T10:00:00+00:00", "old1")]
    assert await sync.async_sync() == 0
    assert uploads == []

    mock_client.get_feed.side_effect = [
        [
            _post("p2", "2026-10-28T15:30:00+00:00", "b1"),
            _post("p1", "2026-10-28T14:05:09+00:00", "a1", "a2"),
            _post("old", "2026-10-20T10:00:00+00:00", "old1"),
        ],
    ]
    assert await sync.async_sync() == 3
    # Oldest first, dated from the post, into the chosen album.
    assert [u["taken"] for u in uploads] == [
        "2026:10:28 14:05:09",
        "2026:10:28 14:05:09",
        "2026:10:28 15:30:00",
    ]
    assert {u["album"] for u in uploads} == {ALBUM}
    assert all(u["type"] == "image/jpeg" for u in uploads)
    assert not any((media_dir / UPLOAD_DIR).iterdir()), "temp files must be deleted"

    state = hass.states.get("sensor.famly_pat_parent_photos_uploaded_to_immich")
    assert state.state == "3"
    assert state.attributes["last_error"] is None

    # Same feed again: nothing re-uploaded.
    mock_client.get_feed.side_effect = None
    mock_client.get_feed.return_value = [
        _post("p2", "2026-10-28T15:30:00+00:00", "b1"),
        _post("old", "2026-10-20T10:00:00+00:00", "old1"),
    ]
    assert await sync.async_sync() == 0
    assert len(uploads) == 3


@pytest.mark.freeze_time(NOW)
async def test_backfill_imports_history(
    hass, mock_client, config_entry, immich_entry, uploads, no_pause
):
    sync = await _setup(
        hass, config_entry, mock_client, immich_entry,
        **{CONF_BACKFILL_SINCE: "2026-10-01"},
    )
    mock_client.get_feed.side_effect = [
        [
            _post("p3", "2026-10-28T14:00:00+00:00", "c1"),
            _post("p2", "2026-10-02T14:00:00+00:00", "b1"),
        ],
        [
            _post("p1", "2026-09-15T14:00:00+00:00", "a1"),  # before the import date
        ],
    ]
    assert await sync.async_sync() == 2
    assert [u["taken"][:10] for u in uploads] == ["2026:10:02", "2026:10:28"]
    assert sync.backfill_done_for == "2026-10-01"


@pytest.mark.freeze_time(NOW)
async def test_pinned_old_post_does_not_stop_scan(
    hass, mock_client, config_entry, immich_entry, uploads, no_pause
):
    sync = await _setup(hass, config_entry, mock_client, immich_entry)
    await sync.async_sync()  # establish starting point = NOW
    sync.newest_synced = datetime.fromisoformat("2026-10-28T12:00:00+00:00")
    mock_client.get_feed.return_value = [
        _post("pinned", "2026-09-01T09:00:00+00:00", "pin1"),
        _post("new", "2026-10-28T15:00:00+00:00", "n1"),
        _post("older", "2026-10-10T09:00:00+00:00", "o1"),
    ]
    assert await sync.async_sync() == 1
    assert uploads[0]["name"].startswith("famly_20261028_")


@pytest.mark.freeze_time(NOW)
async def test_failed_upload_is_retried_and_cleaned_up(
    hass, mock_client, config_entry, immich_entry, media_dir, no_pause
):
    attempts: list[str] = []

    async def _broken(call: ServiceCall) -> None:
        attempts.append(call.data["file"]["media_content_id"])
        raise HomeAssistantError("Immich is down")

    hass.services.async_register("immich", "upload_file", _broken)
    sync = await _setup(hass, config_entry, mock_client, immich_entry)
    await sync.async_sync()
    sync.newest_synced = datetime.fromisoformat("2026-10-28T12:00:00+00:00")
    mock_client.get_feed.return_value = [_post("p", "2026-10-28T15:00:00+00:00", "x1")]

    assert await sync.async_sync() == 0
    # Retried with growing back-off, then stopped until the next hourly run.
    assert len(attempts) == 6
    assert [w for w in no_pause if w >= 30] == [30.0, 60.0, 120.0, 240.0, 480.0]
    assert sync.pending == 1
    assert "Immich is down" in sync.last_error
    assert "x1" not in sync.uploaded
    assert not any((media_dir / UPLOAD_DIR).iterdir())


async def test_sync_entities_absent_when_disabled(hass, mock_client, config_entry):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("button.famly_pat_parent_sync_photos_to_immich") is None
    assert config_entry.runtime_data.photo_sync.enabled is False


async def test_options_flow(hass, mock_client, config_entry, immich_entry):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IMMICH_ALBUM: ALBUM}
    )
    assert result["errors"] == {"base": "immich_required"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_IMMICH_ENTRY: immich_entry.entry_id, CONF_IMMICH_ALBUM: f"https://x/albums/{ALBUM}"},
    )
    await hass.async_block_till_done()
    assert config_entry.options == {
        CONF_IMMICH_ENTRY: immich_entry.entry_id,
        CONF_IMMICH_ALBUM: ALBUM,
    }
    # Reloaded with sync on: the button now exists.
    assert hass.states.get("button.famly_pat_parent_sync_photos_to_immich")


@pytest.mark.freeze_time(NOW)
async def test_paging_sends_rfc3339_older_than(
    hass, mock_client, config_entry, immich_entry, uploads, no_pause
):
    sync = await _setup(
        hass, config_entry, mock_client, immich_entry, **{CONF_BACKFILL_SINCE: "2026-10-01"}
    )
    mock_client.get_feed.side_effect = [
        [_post("p2", "2026-10-28T14:00:00+00:00", "b1")],
        [_post("p1", "2026-10-05T14:00:00+00:00", "a1")],
        [],
    ]
    assert await sync.async_sync() == 2
    second = mock_client.get_feed.call_args_list[1].args
    assert second[0] == "fi-p2"
    assert second[1] == "2026-10-28T14:00:00Z"
    assert sync.stats["feed_items"] == 2


@pytest.mark.freeze_time(NOW)
async def test_empty_feed_does_not_finish_import(
    hass, mock_client, config_entry, immich_entry, uploads, no_pause
):
    sync = await _setup(
        hass, config_entry, mock_client, immich_entry, **{CONF_BACKFILL_SINCE: "2026-10-01"}
    )
    mock_client.get_feed.return_value = []
    assert await sync.async_sync() == 0
    assert sync.backfill_done_for is None


@pytest.mark.freeze_time(NOW)
async def test_diagnostics_show_shape_not_content(
    hass, mock_client, config_entry, immich_entry, no_pause
):
    from custom_components.famly.diagnostics import async_get_config_entry_diagnostics

    await _setup(hass, config_entry, mock_client, immich_entry)
    mock_client.get_feed.return_value = [_post("p", "2026-10-28T14:00:00+00:00", "a1")]
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    sample = diag["feed_probe"]["sample"][0]
    assert sample["photos_recognised"] == 1
    assert sample["createdDate"] == "9999-99-99 99:99:99.999999"
    text = str(diag)
    assert "painted" not in text and "secret" not in text and "parent@example.com" not in text


# --------------------------------------------------- truncated downloads


def _big_jpeg() -> bytes:
    import random

    rnd = random.Random(1)
    img = Image.new("RGB", (400, 300))
    img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(400 * 300)])
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


async def test_download_reads_whole_body_across_chunks(socket_enabled):
    """Regression: StreamReader.read(n) returned only the first chunk."""
    import asyncio

    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from custom_components.famly.api import FamlyClient

    payload = _big_jpeg()

    async def handler(request):
        resp = web.StreamResponse(headers={"Content-Type": "image/jpeg"})
        resp.content_length = len(payload)
        await resp.prepare(request)
        for i in range(0, len(payload), 4096):
            await resp.write(payload[i : i + 4096])
            await asyncio.sleep(0.001)
        return resp

    app = web.Application()
    app.router.add_get("/img.jpg", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        client = FamlyClient(session, "https://app.famly.co", "a", "b")
        body, ctype = await client.download(str(server.make_url("/img.jpg")))
    assert body == payload
    assert ctype == "image/jpeg"


async def test_download_cut_off_raises(socket_enabled):
    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from custom_components.famly.api import FamlyClient, FamlyConnectionError

    payload = _big_jpeg()

    async def handler(request):
        resp = web.StreamResponse(headers={"Content-Type": "image/jpeg"})
        resp.content_length = len(payload)
        await resp.prepare(request)
        await resp.write(payload[: len(payload) // 2])
        request.transport.close()
        return resp

    app = web.Application()
    app.router.add_get("/img.jpg", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        client = FamlyClient(session, "https://app.famly.co", "a", "b")
        with pytest.raises(FamlyConnectionError):
            await client.download(str(server.make_url("/img.jpg")))


def test_write_photo_file_rejects_truncated(tmp_path: Path):
    from custom_components.famly.photo_sync import BrokenImageError

    data = _big_jpeg()
    with pytest.raises(BrokenImageError):
        write_photo_file(tmp_path / "x.jpg", data[: len(data) // 2], datetime.now(UTC))


@pytest.mark.freeze_time(NOW)
async def test_truncated_photo_is_not_uploaded(
    hass, mock_client, config_entry, immich_entry, uploads, media_dir, no_pause
):
    sync = await _setup(hass, config_entry, mock_client, immich_entry)
    await sync.async_sync()
    sync.newest_synced = datetime.fromisoformat("2026-10-28T12:00:00+00:00")
    data = _big_jpeg()
    mock_client.download.return_value = (data[: len(data) // 2], "image/jpeg")
    mock_client.get_feed.return_value = [_post("p", "2026-10-28T15:00:00+00:00", "x1")]

    assert await sync.async_sync() == 0
    assert uploads == []
    assert "incomplete or corrupt" in sync.last_error
    assert "x1" not in sync.uploaded
    assert not any((media_dir / UPLOAD_DIR).iterdir())

    # Next run gets the full file and uploads it.
    mock_client.download.return_value = (data, "image/jpeg")
    assert await sync.async_sync() == 1


# ---------------------------------------------------- batched import


def _many_posts(count: int, start_day: int = 2) -> list[dict]:
    """`count` posts, one photo each, newest first, spread over October."""
    posts = []
    for n in range(count):
        day = start_day + (n % 25)
        posts.append(
            _post(f"p{n}", f"2026-10-{day:02d}T{8 + n % 9:02d}:{n % 60:02d}:00+00:00", f"img{n}")
        )
    posts.sort(key=lambda p: p["createdDate"], reverse=True)
    return posts


def _paged(posts: list[dict], size: int = 10):
    """Serve `posts` page by page the way Famly does (cursor = last feedItemId)."""
    pages = [posts[i : i + size] for i in range(0, len(posts), size)]

    async def _get_feed(cursor=None, older_than=None, first=10):
        if cursor is None:
            return pages[0] if pages else []
        for i, page in enumerate(pages):
            if page and page[-1]["feedItemId"] == cursor:
                return pages[i + 1] if i + 1 < len(pages) else []
        return []

    return _get_feed


@pytest.mark.freeze_time(NOW)
async def test_import_runs_in_batches_until_done(
    hass, mock_client, config_entry, immich_entry, uploads, no_pause
):
    sync = await _setup(
        hass, config_entry, mock_client, immich_entry, **{CONF_BACKFILL_SINCE: "2026-10-01"}
    )
    mock_client.get_feed.side_effect = _paged(_many_posts(130))

    assert await sync.async_sync() == 130
    assert len(uploads) == 130
    # Oldest first.
    taken = [u["taken"] for u in uploads]
    assert taken == sorted(taken)
    # A 10s pause after each batch of 20 (none after the last photo).
    assert no_pause.count(10.0) == 6
    assert sync.backfill_done_for == "2026-10-01"
    assert sync.pending == 0
    state = hass.states.get("sensor.famly_pat_parent_photos_uploaded_to_immich")
    assert state.state == "130"
    assert state.attributes["status"] == "idle"


@pytest.mark.freeze_time(NOW)
async def test_rate_limit_waits_and_retries_same_photo(
    hass, mock_client, config_entry, immich_entry, uploads, no_pause
):
    from custom_components.famly.api import FamlyRateLimitError

    sync = await _setup(
        hass, config_entry, mock_client, immich_entry, **{CONF_BACKFILL_SINCE: "2026-10-01"}
    )
    mock_client.get_feed.side_effect = _paged(_many_posts(3))
    good = mock_client.download.return_value
    mock_client.download.side_effect = [
        FamlyRateLimitError("slow down", retry_after=42),
        good,
        good,
        good,
    ]
    assert await sync.async_sync() == 3
    assert 42 in no_pause
    assert sync.last_error is None


@pytest.mark.freeze_time(NOW)
async def test_feed_rate_limit_is_retried(
    hass, mock_client, config_entry, immich_entry, uploads, no_pause
):
    from custom_components.famly.api import FamlyRateLimitError

    sync = await _setup(
        hass, config_entry, mock_client, immich_entry, **{CONF_BACKFILL_SINCE: "2026-10-01"}
    )
    serve = _paged(_many_posts(15))
    calls = {"n": 0}

    async def _flaky(cursor=None, older_than=None, first=10):
        calls["n"] += 1
        if calls["n"] == 2:
            raise FamlyRateLimitError("slow down", retry_after=5)
        return await serve(cursor, older_than, first)

    mock_client.get_feed.side_effect = _flaky
    assert await sync.async_sync() == 15
    assert 5 in no_pause


@pytest.mark.freeze_time(NOW)
async def test_expired_links_trigger_fresh_feed_read(
    hass, mock_client, config_entry, immich_entry, uploads, no_pause
):
    from custom_components.famly.api import FamlyLinkExpiredError

    sync = await _setup(
        hass, config_entry, mock_client, immich_entry, **{CONF_BACKFILL_SINCE: "2026-10-01"}
    )
    mock_client.get_feed.side_effect = _paged(_many_posts(4))
    good = mock_client.download.return_value
    mock_client.download.side_effect = [good, FamlyLinkExpiredError("expired"), good, good, good]

    assert await sync.async_sync() == 4
    first_page_reads = [c for c in mock_client.get_feed.call_args_list if c.args[0] is None]
    assert len(first_page_reads) == 2  # initial walk + one re-read
    assert sync.backfill_done_for == "2026-10-01"


@pytest.mark.freeze_time(NOW)
async def test_broken_photo_is_skipped_not_blocking(
    hass, mock_client, config_entry, immich_entry, uploads, no_pause
):
    sync = await _setup(
        hass, config_entry, mock_client, immich_entry, **{CONF_BACKFILL_SINCE: "2026-10-01"}
    )
    mock_client.get_feed.side_effect = _paged(_many_posts(3))
    good = mock_client.download.return_value
    mock_client.download.side_effect = [(b"not an image", "image/jpeg"), good, good]

    assert await sync.async_sync() == 2
    assert sync.pending == 1
    assert sync.backfill_done_for is None  # retried next run, import not finished
    assert "incomplete or corrupt" in sync.last_error


@pytest.mark.freeze_time(NOW)
async def test_button_starts_sync_in_background(
    hass, mock_client, config_entry, immich_entry, uploads, no_pause
):
    sync = await _setup(
        hass, config_entry, mock_client, immich_entry, **{CONF_BACKFILL_SINCE: "2026-10-01"}
    )
    mock_client.get_feed.side_effect = _paged(_many_posts(5))
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.famly_pat_parent_sync_photos_to_immich"},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(uploads) == 5
    assert sync.status == "idle"
