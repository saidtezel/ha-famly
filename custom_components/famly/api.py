"""Minimal async client for the Famly parent-side API.

This talks to the same endpoints the app.famly.co web client uses. It is not
an official or documented API and may change without notice.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import date
from json import loads as json_loads
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
USER_AGENT = "HomeAssistant-Famly"

# Copied from the app.famly.co web client. authenticateWithPassword returns a
# union, so every field has to be selected through the AuthenticationResult
# fragment rather than directly.
AUTHENTICATE_MUTATION = """
mutation Authenticate($email: EmailAddress!, $password: Password!, $deviceId: DeviceId, $legacy: Boolean) {
  me {
    authenticateWithPassword(
      email: $email
      password: $password
      deviceId: $deviceId
      legacy: $legacy
    ) {
      __typename
      ...AuthenticationResult
    }
    __typename
  }
}

fragment AuthenticationResult on AuthenticationResult {
  status
  __typename
  ... on AuthenticationFailed {
    status
    errorDetails
    errorTitle
    __typename
  }
  ... on AuthenticationSucceeded {
    accessToken
    deviceId
    __typename
  }
  ... on AuthenticationChallenged {
    __typename
    loginId
    deviceId
    expiresAt
    choices {
      hmac
      requiresTwoFactor
      __typename
    }
    requiredMfaSetup {
      availableMethods
      __typename
    }
  }
}
"""

NOTIFICATION_QUERY = """
query NotificationUpdates {
  me {
    notifications { unseen }
    conversations { inbox { unseen { all own star } total { all } } }
  }
}
"""


class FamlyError(Exception):
    """Generic Famly API error."""


class FamlyConnectionError(FamlyError):
    """Network or server problem."""


class FamlyAuthError(FamlyError):
    """Credentials rejected or session no longer valid."""


class FamlyAuthChallengeError(FamlyAuthError):
    """Famly asked for an extra login step (2FA or account choice)."""


class FamlyClient:
    """Async Famly client bound to one login."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        email: str,
        password: str,
        device_id: str | None = None,
        access_token: str | None = None,
        on_token: Callable[[str], Awaitable[None] | None] | None = None,
    ) -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        self._email = email
        self._password = password
        self.device_id = device_id or str(uuid.uuid4())
        self.installation_id = self.device_id
        self.access_token = access_token
        self._on_token = on_token
        self._login_lock = asyncio.Lock()

    # ------------------------------------------------------------------ auth

    async def login(self) -> str:
        """Exchange email/password for an access token."""
        payload = {
            "operationName": "Authenticate",
            "query": AUTHENTICATE_MUTATION,
            "variables": {
                "email": self._email,
                "password": self._password,
                "deviceId": self.device_id,
                "legacy": True,  # what the web app sends
            },
        }
        data = await self._raw(
            "POST", "/graphql", params={"Authenticate": ""}, json=payload, token=None
        )
        if not isinstance(data, dict):
            raise FamlyError(f"Unexpected login response: {_snippet(str(data))}")
        _LOGGER.debug("Famly login response type: %s", _login_typename(data))
        if data.get("errors") and not (data.get("data") or {}).get("me"):
            raise FamlyError(f"Famly rejected the login request: {_first_error(data)}")
        try:
            result = data["data"]["me"]["authenticateWithPassword"]
        except (KeyError, TypeError) as err:
            raise FamlyError(
                f"Unexpected login response: {_snippet(str(data))}"
            ) from err
        if not isinstance(result, dict):
            raise FamlyError(f"Unexpected login response: {_snippet(str(data))}")

        typename = result.get("__typename")
        if typename == "AuthenticationSucceeded" and result.get("accessToken"):
            token: str = result["accessToken"]
            self.access_token = token
            if self._on_token:
                maybe = self._on_token(token)
                if asyncio.iscoroutine(maybe):
                    await maybe
            return token
        if typename == "AuthenticationChallenged":
            raise FamlyAuthChallengeError(
                "Famly asked for an extra login step (two-factor or account "
                "selection), which this integration does not support yet"
            )
        raise FamlyAuthError(
            result.get("errorTitle") or result.get("errorDetails") or "Login failed"
        )

    async def _ensure_token(self) -> str:
        if self.access_token:
            return self.access_token
        async with self._login_lock:
            if not self.access_token:
                await self.login()
        assert self.access_token
        return self.access_token

    # ------------------------------------------------------------- transport

    async def _raw(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        token: str | None,
    ) -> Any:
        # Same minimal header set famly-fetch uses; the web app's extra
        # x-famly-* headers are not needed and may be validated server-side.
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if token:
            headers["x-famly-accesstoken"] = token
        url = f"{self._base}{path}"
        try:
            async with self._session.request(
                method,
                url,
                params=params,
                json=json,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if resp.status in (401, 403):
                    raise _SessionExpired
                if resp.status == 429 or resp.status >= 500:
                    raise FamlyConnectionError(f"Famly returned HTTP {resp.status}")
                text = await resp.text()
                if resp.status >= 400:
                    raise FamlyError(
                        f"HTTP {resp.status} from {path}: {_snippet(text)}"
                    )
                try:
                    return json_loads(text)
                except ValueError as err:
                    raise FamlyError(
                        f"Non-JSON response (HTTP {resp.status}) from {path}: "
                        f"{_snippet(text)}"
                    ) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise FamlyConnectionError(str(err)) from err

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        """Authenticated request; logs in again once if the token has expired."""
        token = await self._ensure_token()
        try:
            data = await self._raw(method, path, params=params, json=json, token=token)
            if _looks_unauthenticated(data):
                raise _SessionExpired
            return data
        except _SessionExpired:
            _LOGGER.debug("Famly session expired, logging in again")
            async with self._login_lock:
                if self.access_token == token:
                    self.access_token = None
                    await self.login()
            try:
                data = await self._raw(
                    method, path, params=params, json=json, token=self.access_token
                )
            except _SessionExpired as err:
                raise FamlyAuthError("Famly rejected the new session") from err
            if _looks_unauthenticated(data):
                raise FamlyAuthError("Famly rejected the new session")
            return data

    async def graphql(
        self, operation: str, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data = await self._request(
            "POST",
            "/graphql",
            params={operation: ""},
            json={
                "operationName": operation,
                "query": query,
                "variables": variables or {},
            },
        )
        if not isinstance(data, dict):
            raise FamlyError(f"Unexpected response for {operation}")
        if data.get("errors") and not data.get("data"):
            raise FamlyError(_first_error(data))
        return data.get("data") or {}

    # ------------------------------------------------------------- endpoints

    async def get_me(self) -> dict[str, Any]:
        return await self._request(
            "GET", "/api/me/me/me", params={"deviceId": self.device_id}
        )

    async def get_child_summary(self, child_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v2/children/{child_id}/summary")

    async def get_calendar(
        self, child_id: str, start: date, end: date
    ) -> list[dict[str, Any]]:
        """Child calendar between two local dates, inclusive."""
        return await self._request(
            "GET",
            "/api/v2/calendar",
            params={
                "type": "RANGE",
                "day": start.isoformat(),
                "to": end.isoformat(),
                "childId": child_id,
            },
        )

    async def get_tagged_images(
        self, child_id: str, limit: int = 1
    ) -> list[dict[str, Any]]:
        return await self._request(
            "GET",
            "/api/v2/images/tagged",
            params={"childId": child_id, "limit": str(limit)},
        )

    async def get_opening_hours(self, institution_id: str) -> list[dict[str, Any]]:
        data = await self._request(
            "GET", f"/api/v2/institutions/{institution_id}/openinghours"
        )
        return data.get("openingHours", []) if isinstance(data, dict) else []

    async def get_notification_counts(self) -> dict[str, int]:
        data = await self.graphql("NotificationUpdates", NOTIFICATION_QUERY)
        me = data.get("me") or {}
        inbox = ((me.get("conversations") or {}).get("inbox")) or {}
        unseen = inbox.get("unseen") or {}
        return {
            "notifications": int((me.get("notifications") or {}).get("unseen") or 0),
            "messages": int(unseen.get("all") or 0),
        }


class _SessionExpired(Exception):
    """Internal: the server rejected the token."""


def _snippet(text: str, limit: int = 300) -> str:
    return " ".join(text.split())[:limit]


def _login_typename(data: dict[str, Any]) -> str | None:
    try:
        return data["data"]["me"]["authenticateWithPassword"]["__typename"]
    except (KeyError, TypeError):
        return None


def _first_error(data: Any) -> str:
    try:
        return str(data["errors"][0].get("message") or "Unknown error")
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""


def _looks_unauthenticated(data: Any) -> bool:
    """Detect GraphQL-style auth errors returned with HTTP 200."""
    if not isinstance(data, dict) or not data.get("errors") or data.get("data"):
        return False
    message = _first_error(data).lower()
    return any(word in message for word in ("unauthor", "unauthenticated", "not logged in", "access token"))
