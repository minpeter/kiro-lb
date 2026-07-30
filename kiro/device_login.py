# -*- coding: utf-8 -*-

# kiro-lb
# https://github.com/minpeter/kiro-lb
# Copyright (C) 2026 minpeter
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Kiro social device-authorization login (Google / GitHub).

Adding an account previously meant placing a credential file on the server by
hand. This runs the device flow instead: the operator opens one link, approves in
a browser, and the resulting refresh token is registered into the pool.

Ported from the kiro-auth TypeScript reference. Three details of this service
break code written against the AWS SSO OIDC flow:

- Pending is HTTP 200 with a ``status`` field, not an error. Treating any 200 as
  success stores ``accessToken: None``.
- Timings are milliseconds, not seconds.
- The response carries ``profileArn`` directly, so no profile lookup is needed.
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

LoginProvider = Literal["Google", "Github"]
_PROVIDERS: Dict[str, LoginProvider] = {"google": "Google", "github": "Github"}

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


async def start_device_login(provider: LoginProvider) -> DeviceFlow:
    """Request a device code and return the flow to show the operator."""
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
    _prune_flows()
    _flows[flow.id] = flow
    logger.info("Started {} device login {}", provider, flow.id)
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


async def poll_device_login(flow_id: str) -> DeviceFlow:
    """Ask upstream once whether the browser approval has completed."""
    flow = get_flow(flow_id)
    if flow.status in ("approved", "failed", "expired"):
        return flow
    if time.time() > flow.expires_at:
        flow.status = "expired"
        flow.detail = "The approval window closed before the login was confirmed"
        return flow

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
