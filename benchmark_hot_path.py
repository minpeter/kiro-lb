# -*- coding: utf-8 -*-
"""Before/after benchmark for the request hot path.

Three local costs were paid on the event loop for every ``/v1`` request, and
this script measures each one with the optimization disabled ("before") and
enabled ("after"), in the same process, against the same data:

1. API key verification. scrypt at n=2**14 reads 16 MiB and costs tens of
   milliseconds; "before" clears the verification cache each iteration, which
   is exactly what the old code did implicitly.
2. Store access. "before" reopens the connection each iteration - mkdir,
   connect, chmod and two PRAGMAs, one of which writes the file header.
3. Payload request logging. "before" pretty-prints the Kiro payload
   unconditionally; "after" asks the logger whether it will keep it first.

Then, with ``--live``, it measures end-to-end time-to-first-token against
claude-opus-5 through the real gateway, which is where the three savings
actually land.

    python benchmark_hot_path.py
    python benchmark_hot_path.py --live --reps 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import tempfile
import time
from pathlib import Path
from typing import Callable

PROMPT = "Reply with the single word: ok"


def _timed(label: str, before: Callable[[], object], after: Callable[[], object], reps: int) -> None:
    def run(fn: Callable[[], object]) -> list[float]:
        fn()
        samples = []
        for _ in range(reps):
            start = time.perf_counter()
            fn()
            samples.append((time.perf_counter() - start) * 1000.0)
        return samples

    old = statistics.median(run(before))
    new = statistics.median(run(after))
    saved = old - new
    factor = (old / new) if new else float("inf")
    print(f"{label:<28} before {old:8.3f} ms   after {new:8.3f} ms   -{saved:7.3f} ms  ({factor:6.1f}x)")


def bench_key_verification(reps: int) -> None:
    from kiro import dashboard

    dashboard.initialize_dashboard_store()
    raw, _ = dashboard.create_data_api_key("benchmark")

    def before() -> object:
        dashboard.invalidate_api_key_cache()
        return dashboard.identify_data_api_key(raw)

    def after() -> object:
        return dashboard.identify_data_api_key(raw)

    _timed("api key verification", before, after, reps)


def bench_store_connection(reps: int) -> None:
    from kiro import store

    with store.connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS bench_probe (id INTEGER PRIMARY KEY)")

    def query() -> object:
        with store.connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM bench_probe").fetchone()

    def before() -> object:
        store.reset_connection_cache()
        return query()

    _timed("store query", before, query, reps)


def bench_payload_log(reps: int) -> None:
    from kiro.debug_logger import DebugLogger

    logger = DebugLogger()
    payload = {
        "conversationState": {
            "currentMessage": {"userInputMessage": {"content": "word " * 60_000}},
            "history": [{"userInputMessage": {"content": "turn " * 2_000}} for _ in range(20)],
        }
    }

    def before() -> object:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        logger.log_kiro_request_body(body)
        return len(body)

    def after() -> object:
        if logger.is_enabled():
            logger.log_kiro_request_body(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        return 0

    _timed("payload request log", before, after, reps)


async def bench_live(model: str, reps: int, base_url: str, api_key: str) -> None:
    import httpx

    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": 16,
        "stream": True,
    }

    ttft: list[float] = []
    total: list[float] = []
    async with httpx.AsyncClient(timeout=120.0) as client:
        for index in range(reps):
            start = time.perf_counter()
            first: float | None = None
            async with client.stream("POST", url, headers=headers, json=body) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: ") and first is None:
                        first = (time.perf_counter() - start) * 1000.0
            elapsed = (time.perf_counter() - start) * 1000.0
            if first is not None:
                ttft.append(first)
                total.append(elapsed)
            print(f"  run {index + 1}: ttft {first:8.1f} ms   total {elapsed:8.1f} ms")

    if ttft:
        print(
            f"\n{model}: median ttft {statistics.median(ttft):.1f} ms, median total {statistics.median(total):.1f} ms"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reps", type=int, default=25)
    parser.add_argument("--live", action="store_true", help="also measure end-to-end ttft through a running gateway")
    parser.add_argument("--model", default="claude-opus-5")
    parser.add_argument("--base-url", default=os.getenv("KIRO_LB_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--api-key", default=os.getenv("KIRO_LB_KEY", ""))
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DASHBOARD_DATA_DIR"] = tmp
        Path(tmp).mkdir(parents=True, exist_ok=True)
        print(f"local hot path, median of {args.reps} reps\n")
        bench_key_verification(args.reps)
        bench_store_connection(args.reps)
        bench_payload_log(args.reps)

        from kiro import store

        store.reset_connection_cache()

    if args.live:
        if not args.api_key:
            raise SystemExit("--live needs --api-key or KIRO_LB_KEY")
        print(f"\nend to end against {args.model}\n")
        asyncio.run(bench_live(args.model, args.reps, args.base_url, args.api_key))


if __name__ == "__main__":
    main()
