# -*- coding: utf-8 -*-
"""Kiro device-authorization login.

Adding an account previously meant placing a credential file on the server by
hand. This runs a device flow instead: the operator opens one link, approves in a
browser, and the resulting credentials are registered into the pool.

Two unrelated services are supported and their contracts are near-inverses, so
the polling logic is deliberately not shared:

- Social (Google / GitHub) on ``prod.us-east-1.auth.desktop.kiro.dev``. Pending
  is HTTP 200 with a ``status`` field, timings are milliseconds, and the response
  carries ``profileArn``, which routes the account to ``runtime.kiro.dev``.
  Refreshing rotates the refresh token, which is updated in the private store.
- Builder ID (AWS SSO OIDC) on ``oidc.{region}.amazonaws.com``. Pending is HTTP
  400 with an ``authorization_pending`` code, timings are seconds, and refreshing
  needs the client registration, so the client id and secret are stored with the
  refresh token in SQLite. The credential has no account-scoped profile; current
  generation adds Kiro CLI's request-scoped service profile instead.

Ported from the kiro-auth TypeScript reference.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Optional

import httpx
from loguru import logger

AUTH_HOST = "https://prod.us-east-1.auth.desktop.kiro.dev"
CLIENT_ID = "kiro-cli"
# The social auth host is pinned to us-east-1, so refreshes resolve there too.
SOCIAL_REGION = "us-east-1"

BUILDER_ID_START_URL = "https://view.awsapps.com/start"
BUILDER_ID_REGION = "us-east-1"
BUILDER_ID_CLIENT_NAME = "kiro-cli"
BUILDER_ID_SCOPES = [
    "codewhisperer:completions",
    "codewhisperer:analysis",
    "codewhisperer:conversations",
]

LoginProvider = Literal["Google", "Github", "BuilderId"]
_PROVIDERS: Dict[str, LoginProvider] = {
    "google": "Google",
    "github": "Github",
    "builder-id": "BuilderId",
    "builderid": "BuilderId",
    "builder_id": "BuilderId",
}

# A pending flow is dropped once its device code can no longer be approved.
_FLOW_GRACE_SECONDS = 60


class DeviceLoginError(RuntimeError):
    pass


@dataclass
class DeviceFlow:
    """One in-progress browser approval."""

    id: str
    provider: LoginProvider
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_at: float
    interval_seconds: float
    status: str = "pending"
    detail: Optional[str] = None
    token: Optional[Dict[str, Any]] = field(default=None, repr=False)
    registration: Optional[Dict[str, Any]] = field(default=None, repr=False)

    def view(self) -> Dict[str, Any]:
        """Client-facing state. Never exposes the token or the device code."""
        return {
            "flowId": self.id,
            "provider": self.provider,
            "status": self.status,
            "detail": self.detail,
            "userCode": self.user_code,
            "verificationUri": self.verification_uri,
            "verificationUriComplete": self.verification_uri_complete,
            "expiresInSeconds": max(0, int(self.expires_at - time.time())),
        }


_flows: Dict[str, DeviceFlow] = {}


def resolve_provider(raw: str) -> LoginProvider:
    provider = _PROVIDERS.get(str(raw or "").strip().lower())
    if provider is None:
        raise ValueError("provider must be google or github")
    return provider


async def _post(client: httpx.AsyncClient, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    response = await client.post(f"{AUTH_HOST}{path}", json=body, timeout=20)
    if response.status_code >= 400:
        message = response.text
        try:
            message = response.json().get("message", message)
        except Exception:
            pass
        raise DeviceLoginError(f"HTTP {response.status_code}: {message}")
    return response.json()


async def _start_social_login(provider: LoginProvider) -> DeviceFlow:
    async with httpx.AsyncClient() as client:
        payload = await _post(
            client,
            "/oauth/device/authorization",
            {"clientId": CLIENT_ID, "loginProvider": provider},
        )

    # Milliseconds upstream; seconds everywhere in this codebase.
    expires_in = float(payload.get("expiresInMilliseconds") or 300_000) / 1000
    interval = float(payload.get("intervalInMilliseconds") or 5_000) / 1000

    flow = DeviceFlow(
        id=secrets.token_urlsafe(12),
        provider=provider,
        device_code=str(payload["deviceCode"]),
        user_code=str(payload.get("userCode", "")),
        verification_uri=str(payload.get("verificationUri", "")),
        verification_uri_complete=str(payload.get("verificationUriComplete", "")),
        expires_at=time.time() + expires_in,
        interval_seconds=max(1.0, interval),
    )
    return flow


def get_flow(flow_id: str) -> DeviceFlow:
    flow = _flows.get(str(flow_id))
    if flow is None:
        raise KeyError("Unknown or expired login flow")
    return flow


def discard_flow(flow_id: str) -> None:
    _flows.pop(str(flow_id), None)


def _prune_flows() -> None:
    cutoff = time.time() - _FLOW_GRACE_SECONDS
    for flow_id, flow in list(_flows.items()):
        if flow.expires_at < cutoff:
            _flows.pop(flow_id, None)


async def _poll_social_login(flow: DeviceFlow) -> DeviceFlow:
    async with httpx.AsyncClient() as client:
        try:
            payload = await _post(
                client,
                "/oauth/device/poll",
                {"clientId": CLIENT_ID, "deviceCode": flow.device_code},
            )
        except DeviceLoginError as exc:
            flow.status = "failed"
            flow.detail = str(exc)
            return flow

    access_token = payload.get("accessToken")
    if access_token:
        flow.token = {
            "accessToken": access_token,
            "refreshToken": payload.get("refreshToken"),
            "profileArn": payload.get("profileArn"),
            "identityProvider": payload.get("identityProvider"),
            "expiresIn": payload.get("expiresIn"),
        }
        flow.status = "approved"
        flow.detail = None
        return flow

    # Pending arrives as HTTP 200 carrying a status, so the absence of an
    # exception says nothing about success.
    upstream_status = str(payload.get("status") or "authorization_pending")
    if upstream_status != "authorization_pending":
        flow.status = "expired" if "expired" in upstream_status else "failed"
        flow.detail = f"Device authorization {upstream_status}"
    return flow


async def _oidc_call(region: str, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Call AWS SSO OIDC, raising DeviceLoginError carrying the error code.

    Pending is signalled by HTTP 400 with ``error: authorization_pending``, so the
    code has to survive the failure path rather than the success path.
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(f"https://oidc.{region}.amazonaws.com{path}", json=body, timeout=20)
    payload: Dict[str, Any] = {}
    if response.text:
        try:
            payload = response.json()
        except Exception:
            payload = {}
    if response.status_code >= 400:
        code = payload.get("error") or response.headers.get("x-amzn-errortype", "").split(":")[0]
        message = payload.get("error_description") or payload.get("message") or response.text
        raise DeviceLoginError(f"{code or f'HTTP_{response.status_code}'}: {message}")
    return payload


async def _start_builder_id_login() -> DeviceFlow:
    registration = await _oidc_call(
        BUILDER_ID_REGION,
        "/client/register",
        {"clientName": BUILDER_ID_CLIENT_NAME, "clientType": "public", "scopes": BUILDER_ID_SCOPES},
    )
    authorization = await _oidc_call(
        BUILDER_ID_REGION,
        "/device_authorization",
        {
            "clientId": registration["clientId"],
            "clientSecret": registration["clientSecret"],
            "startUrl": BUILDER_ID_START_URL,
        },
    )

    # Seconds here, unlike the social flow's milliseconds.
    expires_in = float(authorization.get("expiresIn") or 600)
    interval = float(authorization.get("interval") or 5)

    flow = DeviceFlow(
        id=secrets.token_urlsafe(12),
        provider="BuilderId",
        device_code=str(authorization["deviceCode"]),
        user_code=str(authorization.get("userCode", "")),
        verification_uri=str(authorization.get("verificationUri", "")),
        verification_uri_complete=str(authorization.get("verificationUriComplete", "")),
        expires_at=time.time() + expires_in,
        interval_seconds=max(1.0, interval),
        registration={
            "clientId": registration["clientId"],
            "clientSecret": registration["clientSecret"],
            "region": BUILDER_ID_REGION,
        },
    )
    return flow


async def _poll_builder_id_login(flow: DeviceFlow) -> DeviceFlow:
    assert flow.registration is not None
    try:
        payload = await _oidc_call(
            flow.registration["region"],
            "/token",
            {
                "clientId": flow.registration["clientId"],
                "clientSecret": flow.registration["clientSecret"],
                "deviceCode": flow.device_code,
                "grantType": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
    except DeviceLoginError as exc:
        reason = str(exc)
        if "authorization_pending" in reason or "AuthorizationPending" in reason:
            return flow
        if "slow_down" in reason or "SlowDown" in reason:
            flow.interval_seconds += 5
            return flow
        flow.status = "expired" if "expired_token" in reason or "ExpiredToken" in reason else "failed"
        flow.detail = reason
        return flow

    access_token = payload.get("accessToken")
    if not access_token:
        flow.status = "failed"
        flow.detail = "Builder ID returned no access token"
        return flow

    flow.token = {
        "accessToken": access_token,
        "refreshToken": payload.get("refreshToken"),
        "expiresIn": payload.get("expiresIn"),
        # Builder ID cannot obtain a profile; ListAvailableProfiles answers 403.
        # Sending an empty profileArn upstream fails the request outright, so the
        # account must carry none at all.
        "profileArn": None,
    }
    flow.status = "approved"
    flow.detail = None
    return flow


async def start_device_login(provider: LoginProvider) -> DeviceFlow:
    """Request a device code and return the flow to show the operator."""
    flow = await (_start_builder_id_login() if provider == "BuilderId" else _start_social_login(provider))
    _prune_flows()
    _flows[flow.id] = flow
    logger.info("Started {} device login {}", provider, flow.id)
    return flow


async def poll_device_login(flow_id: str) -> DeviceFlow:
    """Ask upstream once whether the browser approval has completed."""
    flow = get_flow(flow_id)
    if flow.status in ("approved", "failed", "expired"):
        return flow
    if time.time() > flow.expires_at:
        flow.status = "expired"
        flow.detail = "The approval window closed before the login was confirmed"
        return flow

    if flow.provider == "BuilderId":
        return await _poll_builder_id_login(flow)
    return await _poll_social_login(flow)


async def await_approval(flow_id: str, timeout_seconds: float) -> DeviceFlow:
    """Poll at the upstream-advertised interval until resolved or timed out."""
    flow = get_flow(flow_id)
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        flow = await poll_device_login(flow_id)
        if flow.status != "pending":
            return flow
        await asyncio.sleep(flow.interval_seconds)
    return flow


def _expires_at(expires_in: Any) -> str:
    seconds = float(expires_in or 3600)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + seconds))


def internal_credentials(flow: DeviceFlow) -> Dict[str, Any]:
    """Build the private credential document stored for an approved login."""
    if not flow.token:
        raise ValueError(f"{flow.provider} login is not approved")
    document: Dict[str, Any] = {
        "refreshToken": flow.token.get("refreshToken"),
        "accessToken": flow.token.get("accessToken"),
        "expiresAt": _expires_at(flow.token.get("expiresIn")),
        "region": SOCIAL_REGION,
    }
    if flow.provider == "BuilderId":
        if not flow.registration:
            raise ValueError("Builder ID login has no client registration")
        document.update(
            region=flow.registration["region"],
            clientId=flow.registration["clientId"],
            clientSecret=flow.registration["clientSecret"],
            startUrl=BUILDER_ID_START_URL,
        )
    else:
        for source, target in (("profileArn", "profileArn"), ("identityProvider", "identityProvider")):
            if flow.token.get(source):
                document[target] = flow.token[source]
    return document
