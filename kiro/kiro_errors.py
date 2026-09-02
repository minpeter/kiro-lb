# -*- coding: utf-8 -*-
"""
Kiro API error enhancement and user-friendly message formatting.

This module provides a centralized system for enhancing cryptic Kiro API errors
with clear, actionable, user-friendly messages.

Architecture:
- KiroErrorReason: Enum of known error reasons from Kiro API
- KiroErrorInfo: Structured information about an enhanced error
- enhance_kiro_error(): Analyzes error JSON and returns enhanced message

Example:
    >>> error_json = {"message": "Input is too long.", "reason": "CONTENT_LENGTH_EXCEEDS_THRESHOLD"}
    >>> error_info = enhance_kiro_error(error_json)
    >>> print(error_info.user_message)
    "Model context limit reached. Conversation size exceeds model capacity."
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional

#: Reason code for an upstream account suspension. The runtime host sends it in
#: the ``reason`` field; the legacy ``q.*`` host sends the same verdict with
#: ``reason: null`` and only the message to go on, so both are recognized.
SUSPENSION_REASON = "TEMPORARILY_SUSPENDED"

_SUSPENSION_MARKERS = ("temporarily suspended", "temporarily is suspended", "locked your account", "locked it as a")

#: Synthetic reason code for a refresh token the auth host has rejected outright.
#: Not an upstream value: the refresh endpoint answers with a bare status and
#: ``{"message": "Bad credentials"}``, so the pool needs its own label to tell
#: this apart from the data-plane refusals that share a status code.
CREDENTIAL_DEAD_REASON = "CREDENTIAL_REJECTED"

#: Statuses from the token endpoint that mean the credential itself is finished.
#: 401 is the observed verdict ("Bad credentials"); 400 ``invalid_grant`` is the
#: OIDC spelling of the same thing. Both are finalized only after the retry with
#: freshly reloaded source credentials has already failed, so reaching here means
#: no stored copy of the token works any more.
CREDENTIAL_DEAD_STATUSES = (400, 401)


def is_credential_dead_status(status_code: int) -> bool:
    """Report whether a token-endpoint refusal means the credential is dead.

    Scoped to the refresh endpoints on purpose. The same status from the data
    plane means something else entirely - a 400 there is a malformed payload -
    so callers must only ask this about a token refresh response.
    """
    return status_code in CREDENTIAL_DEAD_STATUSES


def is_suspension_error(status_code: int, message: Optional[str], reason: Optional[str] = None) -> bool:
    """Report whether an upstream refusal means the account itself is locked.

    The reason code is conclusive on its own: the upstream sets that field, so it
    cannot be an echo of anything the client sent, whatever status accompanies it.

    The wording heuristic is what needs the status. Only an authorization refusal
    carries this verdict; the same sentence inside a validation error is the
    payload being quoted back, not a statement about the account.
    """
    if reason == SUSPENSION_REASON:
        return True
    if status_code != 403:
        return False
    if not message:
        return False
    lowered = message.lower()
    return any(marker in lowered for marker in _SUSPENSION_MARKERS)


@dataclass
class KiroErrorInfo:
    """
    Structured information about a Kiro API error.

    Contains both the enhanced user-friendly message and the original
    error details for logging and debugging.

    Attributes:
        reason: Error reason code from Kiro API (as string, e.g. "CONTENT_LENGTH_EXCEEDS_THRESHOLD")
        user_message: Enhanced, user-friendly message for end users
        original_message: Original message from Kiro API (for logging)
    """

    reason: str
    user_message: str
    original_message: str


def enhance_kiro_error(error_json: Dict[str, Any], status_code: Optional[int] = None) -> KiroErrorInfo:
    """
    Enhances Kiro API error with user-friendly message.

    Takes raw error JSON from Kiro API and returns structured information
    with enhanced, user-friendly messages that help users understand what
    went wrong without technical jargon.

    Args:
        error_json: Parsed JSON from Kiro API error response
                   Expected format: {"message": "...", "reason": "..."}
                   The "reason" field is optional.
        status_code: HTTP status the upstream answered with. Required to reach
                   the suspension verdict, because that verdict is only carried
                   by an authorization refusal - the same wording inside a
                   validation error is an echo of the payload. Omitted, no
                   suspension is claimed: an unknown status is not evidence,
                   and convicting a healthy account is the worse error.

    Returns:
        KiroErrorInfo with enhanced message and original details

    Example:
        >>> error_json = {"message": "Input is too long.", "reason": "CONTENT_LENGTH_EXCEEDS_THRESHOLD"}
        >>> error_info = enhance_kiro_error(error_json)
        >>> print(error_info.user_message)
        "Model context limit reached. Conversation size exceeds model capacity."
        >>> print(error_info.original_message)
        "Input is too long."

    Example (unknown error):
        >>> error_json = {"message": "Something went wrong.", "reason": "UNKNOWN_REASON"}
        >>> error_info = enhance_kiro_error(error_json)
        >>> print(error_info.user_message)
        "Something went wrong. (reason: UNKNOWN_REASON)"
    """
    # Extract original message and reason from Kiro API response.
    # Dict.get() is Any | None; coerce to str so KiroErrorInfo.reason stays str
    # (mypy 1.20+ no longer treats Any | None as Any). Empty strings are kept.
    extracted_message = error_json.get("message")
    original_message: str = extracted_message if isinstance(extracted_message, str) else "Unknown error"

    extracted_reason = error_json.get("reason")
    reason: str = extracted_reason if isinstance(extracted_reason, str) else "UNKNOWN"

    # A suspension may arrive with no reason code (legacy host), so it has to be
    # recognized from the message too before the generic branches collapse it
    # into "unknown error". The status is forwarded rather than hardcoded: it is
    # what separates the verdict from an echo of the request payload.
    if status_code is not None and is_suspension_error(status_code, original_message, error_json.get("reason")):
        return KiroErrorInfo(
            reason=SUSPENSION_REASON,
            user_message=(
                "Account suspended by Kiro. This account is locked upstream and cannot serve requests "
                "until support restores it."
            ),
            original_message=original_message,
        )

    # Map known reasons to user-friendly messages
    if reason == "CONTENT_LENGTH_EXCEEDS_THRESHOLD":
        # Context limit exceeded - conversation is too long
        user_message = "Model context limit reached. Conversation size exceeds model capacity."

    elif reason == "MONTHLY_REQUEST_COUNT":
        # Monthly request limit exceeded - account quota exhausted
        user_message = "Monthly request limit exceeded. Account has reached its monthly quota."

    elif reason == "INVALID_MODEL_ID":
        # Invalid model name or subscription tier insufficient
        user_message = "Invalid model ID or insufficient subscription level to use it."

    elif original_message == "Improperly formed request." and reason in (None, "UNKNOWN", "null"):
        # Generic 400 error
        user_message = (
            "Kiro API rejected the request. If problem persists, open issue with info and attached debug logs at:"
            "https://github.com/minpeter/kiro-lb-python/issues"
        )

    # Future error enhancements can be added here:
    # elif reason == "RATE_LIMIT_EXCEEDED":
    #     user_message = "Rate limit exceeded. Too many requests in a short time."
    # elif reason == "INVALID_MODEL":
    #     user_message = "Invalid model specified. The requested model is not available."

    else:
        # Unknown error or no enhancement available
        # Keep original message and append reason if present
        if "reason" in error_json and reason != "UNKNOWN":
            user_message = f"{original_message} (reason: {reason})"
        else:
            user_message = original_message

    return KiroErrorInfo(reason=reason, user_message=user_message, original_message=original_message)
