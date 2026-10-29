"""Client tests: login handling and transparent re-login."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.famly.api import (
    FamlyAuthChallengeError,
    FamlyAuthError,
    FamlyClient,
    _SessionExpired,
)


def _client(**kw) -> FamlyClient:
    return FamlyClient(MagicMock(), "https://app.famly.co", "a@b.c", "pw", **kw)


def _login_ok(token="tok"):
    return {
        "data": {
            "me": {
                "authenticateWithPassword": {
                    "__typename": "AuthenticationSucceeded",
                    "status": "OK",
                    "accessToken": token,
                }
            }
        }
    }


async def test_login_success_stores_and_reports_token():
    seen = []
    client = _client(on_token=seen.append)
    client._raw = AsyncMock(return_value=_login_ok("t1"))
    assert await client.login() == "t1"
    assert client.access_token == "t1"
    assert seen == ["t1"]
    sent = client._raw.call_args.kwargs["json"]
    assert sent["variables"]["email"] == "a@b.c"
    assert sent["variables"]["deviceId"] == client.device_id


async def test_login_failed():
    client = _client()
    client._raw = AsyncMock(
        return_value={
            "data": {
                "me": {
                    "authenticateWithPassword": {
                        "__typename": "AuthenticationFailed",
                        "errorTitle": "Wrong password",
                    }
                }
            }
        }
    )
    with pytest.raises(FamlyAuthError, match="Wrong password"):
        await client.login()


async def test_login_challenge():
    client = _client()
    client._raw = AsyncMock(
        return_value={
            "data": {
                "me": {
                    "authenticateWithPassword": {
                        "__typename": "AuthenticationChallenged",
                        "choices": [],
                    }
                }
            }
        }
    )
    with pytest.raises(FamlyAuthChallengeError):
        await client.login()


async def test_expired_token_relogs_once():
    client = _client(access_token="old")
    client._raw = AsyncMock(
        side_effect=[_SessionExpired(), _login_ok("new"), {"ok": True}]
    )
    assert await client.get_me() == {"ok": True}
    assert client.access_token == "new"
    assert client._raw.call_args_list[2].kwargs["token"] == "new"


async def test_relogin_rejected_raises_auth_error():
    client = _client(access_token="old")
    client._raw = AsyncMock(
        side_effect=[_SessionExpired(), _login_ok("new"), _SessionExpired()]
    )
    with pytest.raises(FamlyAuthError):
        await client.get_me()


async def test_no_token_logs_in_first():
    client = _client()
    client._raw = AsyncMock(side_effect=[_login_ok("t"), {"me": 1}])
    assert await client.get_me() == {"me": 1}


async def test_graphql_error_on_login_is_reported_not_auth():
    from custom_components.famly.api import FamlyError

    client = _client()
    client._raw = AsyncMock(
        return_value={"errors": [{"message": "Unknown field"}], "data": None}
    )
    with pytest.raises(FamlyError, match="Unknown field") as exc:
        await client.login()
    assert not isinstance(exc.value, FamlyAuthError)
