# -*- coding: utf-8 -*-
"""Run CPU-bound work off the asyncio event loop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


async def run_in_worker(fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """Run ``fn`` in a worker thread and return its result.

    History trim and tiktoken are CPU-bound. Running them on the asyncio
    thread stalls /health and the dashboard until they finish.
    """
    return await asyncio.to_thread(fn, *args, **kwargs)
