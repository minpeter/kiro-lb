# -*- coding: utf-8 -*-
"""Live probe: what unit does Kiro's CONTENT_LENGTH_EXCEEDS_THRESHOLD count?

Calibrate against generateAssistantResponse / claude-haiku-4.5 / no tools,
matching the original ASCII bisect in kiro/config.py. Distinguishes a size
reject from a later context-window failure in the stream.

Not collected by pytest. Run inside the gateway container so it uses the
account pool without going through the local byte guard:

    python manual_payload_limit_probe.py --dry-run
    python manual_payload_limit_probe.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from typing import Any, Dict, Optional, Tuple

# Live path imports are deferred so --dry-run works without gateway deps.

MODEL_ID = "claude-haiku-4.5"
CURRENT_PROMPT = "Reply with the single word OK."
HANGUL = "가"
ASTRAL = "😀"  # U+1F600: 1 codepoint, 2 UTF-16 units, 4 UTF-8 bytes
ASCII_PASS = 1_085_435
ASCII_FAIL = 1_086_459
HANGUL_OVER = 1_200_000
ASTRAL_COUNT_TARGET_CHARS = 600_000  # of the serialized JSON, padded with emoji


def compact_dumps(payload: Dict[str, Any], *, ensure_ascii: bool) -> str:
    return json.dumps(payload, ensure_ascii=ensure_ascii, separators=(",", ":"))


def measures(text: str) -> Dict[str, int]:
    utf16_units = len(text.encode("utf-16-le")) // 2
    return {
        "chars": len(text),
        "utf8_bytes": len(text.encode("utf-8")),
        "utf16_units": utf16_units,
        "escaped_ascii_bytes": len(json.dumps(json.loads(text), ensure_ascii=True, separators=(",", ":")).encode()),
    }


def build_payload(filler: str, *, conversation_id: str, profile_arn: Optional[str]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "conversationState": {
            "chatTriggerType": "MANUAL",
            "conversationId": conversation_id,
            "currentMessage": {
                "userInputMessage": {
                    "content": CURRENT_PROMPT,
                    "modelId": MODEL_ID,
                    "origin": "AI_EDITOR",
                }
            },
            "history": [
                {
                    "userInputMessage": {
                        "content": filler,
                        "modelId": MODEL_ID,
                        "origin": "AI_EDITOR",
                    }
                },
                {"assistantResponseMessage": {"content": "OK"}},
            ],
        }
    }
    if profile_arn:
        payload["profileArn"] = profile_arn
    return payload


def pad_payload(
    ch: str,
    *,
    conversation_id: str,
    profile_arn: Optional[str],
    target_chars: Optional[int] = None,
    target_utf8: Optional[int] = None,
    target_escaped: Optional[int] = None,
    ensure_ascii: bool = False,
) -> Tuple[Dict[str, Any], str]:
    """Pad homogeneous filler so the compact JSON hits an exact size."""
    specified = [v is not None for v in (target_chars, target_utf8, target_escaped)]
    if sum(specified) != 1:
        raise ValueError("exactly one of target_chars, target_utf8, target_escaped is required")

    skeleton_payload = build_payload("", conversation_id=conversation_id, profile_arn=profile_arn)
    skeleton = compact_dumps(skeleton_payload, ensure_ascii=ensure_ascii)
    escaped_skeleton = compact_dumps(skeleton_payload, ensure_ascii=True)
    sent_unit = json.dumps(ch, ensure_ascii=ensure_ascii, separators=(",", ":"))[1:-1]
    escaped_unit = json.dumps(ch, ensure_ascii=True, separators=(",", ":"))[1:-1]
    unit_chars = len(sent_unit)
    unit_utf8 = len(sent_unit.encode("utf-8"))
    unit_escaped = len(escaped_unit)
    if unit_chars == 0 or unit_utf8 == 0 or unit_escaped == 0:
        raise ValueError("filler character serialized to empty")

    if target_chars is not None:
        need = target_chars - len(skeleton)
        n, rem = divmod(need, unit_chars)
    elif target_utf8 is not None:
        need = target_utf8 - len(skeleton.encode("utf-8"))
        n, rem = divmod(need, unit_utf8)
    else:
        need = target_escaped - len(escaped_skeleton)
        n, rem = divmod(need, unit_escaped)

    if n < 0:
        raise ValueError("skeleton already exceeds target")

    filler = ch * n + ("x" * rem)
    payload = build_payload(filler, conversation_id=conversation_id, profile_arn=profile_arn)
    text = compact_dumps(payload, ensure_ascii=ensure_ascii)
    if target_chars is not None and len(text) != target_chars:
        raise RuntimeError(f"char pad missed: got {len(text)} want {target_chars}")
    if target_utf8 is not None and len(text.encode("utf-8")) != target_utf8:
        raise RuntimeError(f"utf8 pad missed: got {len(text.encode('utf-8'))} want {target_utf8}")
    if target_escaped is not None:
        got = len(compact_dumps(payload, ensure_ascii=True))
        if got != target_escaped:
            raise RuntimeError(f"escaped pad missed: got {got} want {target_escaped}")
    return payload, text


def classify_failure(status: int, body: str) -> str:
    reason = ""
    message = body
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            reason = str(parsed.get("reason") or "")
            message = str(parsed.get("message") or body)
    except Exception:
        pass
    lowered = f"{reason} {message}".lower()
    if status == 400 and reason == "CONTENT_LENGTH_EXCEEDS_THRESHOLD":
        return "size_reject"
    if "content_length_exceeds_threshold" in lowered or "content length exceeds threshold" in lowered:
        return "size_reject"
    if "input is too long" in lowered:
        return "size_reject"
    if "context" in lowered or "too many tokens" in lowered:
        return "context_window"
    if status == 200:
        if "exception" in lowered or "error" in lowered:
            return "stream_error"
        return "accepted"
    return f"other_http_{status}"


async def send_once(
    *,
    url: str,
    headers: dict,
    payload: Dict[str, Any],
    body: bytes,
    timeout: float,
) -> Dict[str, Any]:
    import httpx

    started = time.perf_counter()
    preview = ""
    status = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=30.0, read=timeout, write=60.0, pool=30.0)) as client:
            async with client.stream("POST", url, headers=headers, content=body) as response:
                status = response.status_code
                # Size rejects are HTTP 400 with a JSON body. A 200 means the
                # payload passed the length check; do not wait for generation.
                if status == 200:
                    await response.aclose()
                    preview = ""
                else:
                    preview = (await response.aread()).decode("utf-8", errors="replace")[:2000]
    except httpx.HTTPError as exc:
        return {
            "status": status or None,
            "elapsed_s": round(time.perf_counter() - started, 3),
            "class": f"transport_{type(exc).__name__}",
            "preview": str(exc)[:500],
        }
    return {
        "status": status,
        "elapsed_s": round(time.perf_counter() - started, 3),
        "class": classify_failure(status, preview),
        "preview": preview,
    }


async def pick_account() -> Tuple[Any, Any]:
    from kiro.account_manager import AccountManager

    manager = AccountManager()
    await manager.load_credentials()
    account_ids = list(manager._accounts)
    if not account_ids:
        raise RuntimeError("no accounts in the gateway store")
    # Pin the first account that initializes. One account, as the handoff asked.
    last_error = ""
    for account_id in account_ids:
        ok = await manager.initialize_account(account_id)
        if not ok:
            last_error = account_id
            continue
        account = manager._accounts[account_id]
        if account.auth_manager is None:
            continue
        return manager, account
    raise RuntimeError(f"could not initialize any account (last={last_error!r})")


async def run(args: argparse.Namespace) -> int:
    conversation_id = str(uuid.UUID("00000000-0000-4000-8000-000000000001"))
    profile_arn = None
    auth = None
    url = ""
    headers: dict = {}

    global MODEL_ID
    MODEL_ID = args.model

    if not args.dry_run:
        _manager, account = await pick_account()
        auth = account.auth_manager
        assert auth is not None
        profile_arn = auth.profile_arn or None
        from kiro.utils import get_kiro_headers

        token = await auth.get_access_token()
        headers = get_kiro_headers(auth, token)
        url = f"{auth.api_host}/generateAssistantResponse"
        print(
            json.dumps(
                {
                    "account_id": account.id,
                    "auth_type": auth.auth_type.name if hasattr(auth, "auth_type") else "unknown",
                    "api_host": auth.api_host,
                    "profile_arn": "present" if profile_arn else "absent",
                    "model": MODEL_ID,
                },
                indent=2,
            )
        )

    if not args.dry_run:
        smoke_payload = build_payload("ping", conversation_id=conversation_id, profile_arn=profile_arn)
        smoke_text = compact_dumps(smoke_payload, ensure_ascii=False)
        smoke = await send_once(
            url=url,
            headers=headers,
            payload=smoke_payload,
            body=smoke_text.encode("utf-8"),
            timeout=args.timeout,
        )
        smoke["name"] = "E0_smoke"
        print(json.dumps(smoke, ensure_ascii=False, indent=2))
        if smoke["class"] not in ("accepted", "context_window"):
            print(json.dumps({"abort": "smoke failed", "smoke": smoke}, indent=2))
            return 1

    if args.suite == "unit":
        experiments = [
            ("E1_ascii_pass", "x", {"target_utf8": ASCII_PASS}, False, "H1/H4 pass"),
            ("E2_ascii_fail", "x", {"target_utf8": ASCII_FAIL}, False, "H1/H4 fail"),
            ("E3_hangul_at_ascii_chars", HANGUL, {"target_chars": ASCII_PASS}, False, "H1/H2 pass, H4 fail"),
            ("E4_hangul_over_chars", HANGUL, {"target_chars": HANGUL_OVER}, False, "H1/H2 fail"),
            ("E5_astral_600k_chars", ASTRAL, {"target_chars": ASTRAL_COUNT_TARGET_CHARS}, False, "H1 pass, H2 fail"),
            (
                "E3b_hangul_escaped_same_chars",
                HANGUL,
                {"target_chars": ASCII_PASS},
                True,
                "same parsed chars as E3, larger Content-Length",
            ),
        ]
    elif args.suite == "wire":
        # Content-Length sweep. Round 1 showed E2 (1,086,459 ASCII) now passes
        # and Hangul/emoji only fail when the UTF-8 body is multi-MB.
        experiments = [
            ("W1_hangul_utf8_at_old_pass", HANGUL, {"target_utf8": ASCII_PASS}, False, "pass if unit is wire bytes"),
            ("W2_hangul_utf8_comment_2m", HANGUL, {"target_utf8": 2_070_175}, False, "original Korean probe size"),
            ("W3_ascii_1_2m", "x", {"target_utf8": 1_200_000}, False, "ASCII above old fail"),
            ("W4_ascii_1_5m", "x", {"target_utf8": 1_500_000}, False, "ASCII 1.5MB"),
            ("W5_ascii_2_07m", "x", {"target_utf8": 2_070_175}, False, "ASCII at Korean-probe bytes"),
            ("W6_emoji_utf8_at_old_pass", ASTRAL, {"target_utf8": ASCII_PASS}, False, "pass if unit is wire bytes"),
            ("W7_hangul_utf8_1_17m", HANGUL, {"target_utf8": 1_170_162}, False, "size that the local guard rejected"),
        ]
    elif args.suite == "entropy":
        alphabet = "abcdefghijklmnopqrstuvwxyz"
        experiments = [
            (
                "N1_cycle_ascii_1550k",
                alphabet,
                {"target_utf8": 1_550_000},
                False,
                "FAIL if tokens (x*1.55M passed at ~194k tok)",
            ),
            (
                "N2_cycle_ascii_180k",
                alphabet,
                {"target_utf8": 180_000},
                False,
                "PASS if ~45k tokens; size similar to Hangul pass chars",
            ),
            (
                "N3_hangul_190k_chars",
                HANGUL,
                {"target_chars": 190_000},
                False,
                "tighten Hangul boundary",
            ),
            (
                "N4_hangul_195k_chars",
                HANGUL,
                {"target_chars": 195_000},
                False,
                "tighten Hangul boundary",
            ),
            (
                "N5_ascii_x_1575k",
                "x",
                {"target_utf8": 1_575_000},
                False,
                "tighten ASCII x boundary",
            ),
        ]
    elif args.suite == "bisect":
        experiments = [
            ("B1_hangul_180k_chars", HANGUL, {"target_chars": 180_000}, False, "near E3b parsed Hangul count"),
            ("B2_hangul_200k_chars", HANGUL, {"target_chars": 200_000}, False, "Hangul char bisect"),
            ("B3_hangul_220k_chars", HANGUL, {"target_chars": 220_000}, False, "Hangul char bisect"),
            ("B4_ascii_1550k", "x", {"target_utf8": 1_550_000}, False, "ASCII bisect"),
            ("B5_ascii_1600k", "x", {"target_utf8": 1_600_000}, False, "ASCII bisect"),
            ("B6_ascii_1650k", "x", {"target_utf8": 1_650_000}, False, "ASCII bisect"),
            ("B7_ascii_1700k", "x", {"target_utf8": 1_700_000}, False, "ASCII bisect"),
        ]
    elif args.suite == "escaped":
        # Hypothesis: upstream re-serializes with ensure_ascii=True (\\uXXXX)
        # and counts that ASCII length. ASCII 1.5MB passed, 2.07MB failed.
        experiments = [
            ("X1_hangul_escaped_1_5m", HANGUL, {"target_utf8": 1_500_000}, True, "pass if escaped-bytes is the unit"),
            ("X2_hangul_escaped_2_07m", HANGUL, {"target_utf8": 2_070_175}, True, "fail if escaped-bytes is the unit"),
            ("X3_ascii_1_8m", "x", {"target_utf8": 1_800_000}, False, "ASCII mid-band"),
            ("X4_ascii_1_9m", "x", {"target_utf8": 1_900_000}, False, "ASCII mid-band"),
            ("X5_ascii_2_0m", "x", {"target_utf8": 2_000_000}, False, "ASCII near fail"),
            ("X6_emoji_escaped_1_5m", ASTRAL, {"target_utf8": 1_500_000}, True, "emoji \\uD83D\\uDE00 expansion"),
            ("X7_hangul_utf8_send_escaped_1_5m", HANGUL, {"target_escaped": 1_500_000}, False, "UTF-8 on wire, 1.5MB if re-escaped"),
            ("X8_hangul_utf8_send_escaped_2_07m", HANGUL, {"target_escaped": 2_070_175}, False, "UTF-8 on wire, 2.07MB if re-escaped"),
        ]
    elif args.suite == "opus2":
        experiments = [
            ("O5_hangul_800k", HANGUL, {"target_chars": 800_000}, False, "above measured context"),
            ("O6_hangul_1m", HANGUL, {"target_chars": 1_000_000}, False, "advertised 1M"),
        ]
    elif args.suite == "opus":
        # Does claude-opus-5 share haiku's ~195k CONTENT_LENGTH cap, or the
        # advertised 1M / measured 666667 context window?
        experiments = [
            ("O1_hangul_195k", HANGUL, {"target_chars": 195_000}, False, "haiku last-pass"),
            ("O2_hangul_250k", HANGUL, {"target_chars": 250_000}, False, "haiku already size-rejected"),
            ("O3_hangul_400k", HANGUL, {"target_chars": 400_000}, False, "well under 666667 context"),
            ("O4_hangul_666667", HANGUL, {"target_chars": 666_667}, False, "measured opus context window"),
            ("O5_hangul_800k", HANGUL, {"target_chars": 800_000}, False, "above measured context"),
            ("O6_hangul_1m", HANGUL, {"target_chars": 1_000_000}, False, "advertised 1M"),
        ]
    else:
        raise ValueError(args.suite)

    results = []
    for name, ch, target, ensure_ascii, prediction in experiments:
        payload, text = pad_payload(
            ch,
            conversation_id=conversation_id,
            profile_arn=profile_arn,
            ensure_ascii=ensure_ascii,
            **target,
        )
        body = text.encode("utf-8")
        row = {
            "name": name,
            "prediction": prediction,
            "ensure_ascii": ensure_ascii,
            "filler_char": ch,
            "content_length": len(body),
            **{f"json_{k}": v for k, v in measures(text).items()},
        }
        if args.dry_run:
            row["class"] = "dry_run"
        else:
            row.update(
                await send_once(url=url, headers=headers, payload=payload, body=body, timeout=args.timeout)
            )
            # A 200 with a context-window error still means the payload was accepted.
            if row["class"] == "accepted" or row["class"] == "context_window":
                row["payload_accepted"] = True
            elif row["class"] == "size_reject":
                row["payload_accepted"] = False
            else:
                row["payload_accepted"] = None
        results.append(row)
        print(json.dumps(row, ensure_ascii=False, indent=2))

    if args.bisect and not args.dry_run and args.suite == "unit":
        # Character-count bisect between the last Hangul accept and first Hangul size-reject.
        lo = ASCII_PASS
        hi = HANGUL_OVER
        e3 = next(r for r in results if r["name"] == "E3_hangul_at_ascii_chars")
        e4 = next(r for r in results if r["name"] == "E4_hangul_over_chars")
        if e3.get("payload_accepted") is not True:
            print(json.dumps({"bisect": "skipped", "reason": "E3 did not accept"}, indent=2))
        elif e4.get("class") != "size_reject":
            print(json.dumps({"bisect": "skipped", "reason": "E4 was not a size_reject", "e4": e4["class"]}, indent=2))
        else:
            last_pass, first_fail = lo, hi
            while hi - lo > 1024:
                mid = (lo + hi) // 2
                payload, text = pad_payload(
                    HANGUL,
                    conversation_id=conversation_id,
                    profile_arn=profile_arn,
                    target_chars=mid,
                    ensure_ascii=False,
                )
                outcome = await send_once(
                    url=url,
                    headers=headers,
                    payload=payload,
                    body=text.encode("utf-8"),
                    timeout=args.timeout,
                )
                outcome["target_chars"] = mid
                outcome["content_length"] = len(text.encode("utf-8"))
                print(json.dumps({"bisect": outcome}, ensure_ascii=False))
                accepted = outcome["class"] in ("accepted", "context_window")
                if accepted:
                    last_pass = mid
                    lo = mid
                elif outcome["class"] == "size_reject":
                    first_fail = mid
                    hi = mid
                else:
                    print(json.dumps({"bisect": "aborted", "reason": outcome}, indent=2))
                    break
            print(json.dumps({"bisect_boundary": {"last_pass_chars": last_pass, "first_fail_chars": first_fail}}, indent=2))

    print(json.dumps({"summary": [{k: r.get(k) for k in ("name", "class", "payload_accepted", "content_length", "json_chars", "json_utf8_bytes", "json_utf16_units")} for r in results]}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print sizes only, do not call upstream")
    parser.add_argument("--bisect", action="store_true", help="after E1-E5, bisect the Hangul character boundary")
    parser.add_argument(
        "--suite", choices=("unit", "wire", "escaped", "bisect", "entropy", "opus", "opus2"), default="unit"
    )
    parser.add_argument("--model", default=MODEL_ID)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
