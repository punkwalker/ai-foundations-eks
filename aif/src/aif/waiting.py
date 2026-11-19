"""Bounded waits. Every wait in aif has a timeout and says why it stopped."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


class WaitTimeoutError(TimeoutError):
    """A wait ran out of time. The message says what was still not ready."""


@dataclass(frozen=True)
class Clock:
    """Time functions, swapped for fakes in tests."""

    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def until[T](
    check: Callable[[], tuple[bool, T]],
    *,
    timeout: float,
    what: str,
    describe: Callable[[T], str],
    interval: float = 5.0,
    max_interval: float = 30.0,
    clock: Clock | None = None,
) -> T:
    """Call `check` until it returns (True, value), then return value.

    The interval doubles after each try, up to `max_interval`.
    On timeout, raise WaitTimeoutError with `describe(last value)`.
    """
    clock = clock or Clock()
    deadline = clock.now() + timeout
    delay = interval
    while True:
        done, value = check()
        if done:
            return value
        remaining = deadline - clock.now()
        if remaining <= 0:
            raise WaitTimeoutError(
                f"timed out after {timeout:.0f}s waiting for {what}: {describe(value)}"
            )
        clock.sleep(min(delay, remaining))
        delay = min(delay * 2, max_interval)
