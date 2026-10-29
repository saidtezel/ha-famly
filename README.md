# Famly for Home Assistant

Unofficial custom integration that brings your child's day at nursery from [Famly](https://famly.co) into Home Assistant: sign-in/out, naps, nappy changes, meals, nursery events, unread messages and the latest tagged photo.

> **Unofficial.** This uses the same private endpoints as the Famly parent web app, not Famly's paid Public API. It may break when Famly changes their app, and automated access is not something Famly's Terms of Use permit. Use it for your own family's data only, poll gently, and accept the risk to your account.

## Install

HACS → ⋮ → **Custom repositories** → add this repository's URL as an *Integration* → install **Famly** → restart Home Assistant.

Manual: copy `custom_components/famly` into your `config/custom_components/` folder and restart.

## Set up

Settings → Devices & services → **Add integration** → Famly, then enter:

- **Email** and **password** for the Famly parent app
- **Famly site**: `famly.co` (UK, US, Nordics) or `famly.de` (DACH)

Credentials are stored in the config entry in Home Assistant (`.storage/core.config_entries`, like every other integration) and are only sent to Famly. The current session token is stored next to them, so restarts don't need a fresh login. When the session expires the integration signs in again on its own; if Famly rejects the password you'll get a re-authentication prompt.

Accounts with two-factor authentication, or logins that have to choose between several accounts, are not supported yet. Setup will tell you if that's the case.

## What you get

One device per child, plus one for the login.

| Entity | What it shows |
|---|---|
| `binary_sensor.<child>_at_nursery` | Signed in right now |
| `binary_sensor.<child>_sleeping` | A nap has started and not ended |
| `binary_sensor.<child>_sick` | Marked sick today |
| `sensor.<child>_status` | `at_nursery`, `picked_up`, `not_arrived`, `sick`, `absent`, `holiday`; key workers and pickup info as attributes |
| `sensor.<child>_room` | Current room (follows room moves) |
| `sensor.<child>_arrived` / `_picked_up` | Today's first sign-in / final sign-out |
| `sensor.<child>_last_nap_start` / `_last_nap_end` | Most recent nap |
| `sensor.<child>_sleep_today` | Minutes slept today, naps as attributes |
| `sensor.<child>_last_nappy_change` | Time of last change; type and note as attributes |
| `sensor.<child>_nappy_changes_today` | Count, with the list as attributes |
| `sensor.<child>_last_meal` / `_meals_today` | Meal name / count |
| `sensor.<child>_last_activity` | The latest log entry, whatever it was |
| `calendar.<child>_nursery` | Nursery events (stay-and-play, closures) and sick/holiday days |
| `image.<child>_latest_photo` | Most recent photo your child is tagged in |
| `event.<child>_activity` | Fires `check_in`, `check_out`, `room_change`, `nap_start`, `nap_end`, `nappy_change`, `meal`, `activity`, `sick` as entries appear |
| `sensor.famly_<you>_unread_messages` / `_unseen_notifications` | Counts from the app's badge |

### Example automation

```yaml
automation:
  - alias: "Nursery: woke up"
    triggers:
      - trigger: state
        entity_id: event.robin_activity
    conditions:
      - condition: template
        value_template: "{{ trigger.to_state.attributes.event_type == 'nap_end' }}"
    actions:
      - action: notify.mobile_app_phone
        data:
          message: "Robin woke up at {{ as_timestamp(trigger.to_state.attributes.end) | timestamp_custom('%H:%M') }}"
```

## Photo sync to Immich (optional)

Pushes every photo the nursery posts in the news feed to your [Immich](https://immich.app) server, using Home Assistant's own Immich integration. All feed photos are sent, not only ones your child is tagged in, so Immich's face recognition can sort them; delete the ones you don't want there.

**Turn it on:** Settings → Devices & services → Famly → **Configure**:

- **Immich server**: pick your existing Immich integration (required; leave empty to turn sync off)
- **Album**: optional; paste the album URL or ID from the Immich web app
- **Also import photos posted since**: optional one-off import of older photos

**How it works**

- Checks the feed **once an hour** (and two minutes after Home Assistant starts). Use the **Sync photos to Immich** button to run it now.
- Each photo is downloaded into `/media/famly_immich_upload/`, handed to `immich.upload_file`, then **deleted immediately**, whether the upload worked or not. Nothing is kept on Home Assistant; leftovers from an interrupted run are cleaned up at startup.
- Famly strips all photo metadata, so the post's date and time is written back into the EXIF data and file timestamp before upload. Photos land on the right day in Immich's timeline.
- Uploaded photo IDs are remembered, so nothing is sent twice. Failed uploads are retried on the next run.
- Without an import date, the first run only records a starting point: you get photos posted from then on. An import is paced at up to 60 photos per hour, oldest first, until it catches up.
- The **Photos uploaded to Immich** sensor shows the running total, with the last run, photos still waiting, the last error and what the last scan found (`last_scan`) as attributes. Each run also logs a one-line summary at info level.
- If nothing arrives, **Download diagnostics** on the integration: it includes the structure of the latest feed page (field names, date formats, counts; no text, names or photo links) and how many photos the integration recognised.

Needs the Immich integration set up in Home Assistant with its `immich.upload_file` action available, and a media folder (Home Assistant OS has `/media` by default).

## Polling

There is no push API, so the integration polls:

- every **2 minutes** during the nursery's opening hours (±45 min) or while your child is signed in
- every **30 minutes** otherwise
- latest photo every 15 minutes, upcoming events hourly, opening hours twice a day

Each fast poll is two small requests per child plus one for unread counts.

## Endpoints used

All on `https://app.famly.co` (or `.de`), authenticated with the `x-famly-accesstoken` header:

| Call | Used for |
|---|---|
| `POST /graphql` `Authenticate` | Email/password → session token |
| `GET /api/me/me/me` | Login id and the children linked to it |
| `GET /api/v2/children/{id}/summary` | Signed-in state, sick/absent, key workers, nap history |
| `GET /api/v2/calendar?type=RANGE&day=&to=&childId=` | Activity log and nursery events |
| `GET /api/v2/images/tagged?childId=&limit=1` | Latest photo |
| `GET /api/v2/institutions/{id}/openinghours` | Polling schedule |
| `POST /graphql` `NotificationUpdates` | Unread counts |
| `GET /api/feed/feed/feed?first=&cursor=&olderThan=` | News feed photos for Immich sync |

## Trademarks

Famly and the Famly logo are trademarks of Famly ApS. This project is not affiliated with or endorsed by Famly; the icon in `custom_components/famly/brand/` is used only to identify the service the integration connects to. It shows in Home Assistant 2026.3 and later.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install pytest-homeassistant-custom-component
pytest
```
