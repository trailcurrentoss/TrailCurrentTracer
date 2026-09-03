"""Module base class — implements the degradation contract from docs/api.md §3.

The rules this class exists to enforce, because acceptance criterion 3 turns
on them:

  1. A module that loses its dependency goes `unavailable` and KEEPS RUNNING
     at a backoff interval. It never exits and never blocks another module.
  2. State transitions are logged ONCE, not once per poll. This is what keeps
     `journalctl -u tracerd` quiet at rest (criterion 5).
  3. A crash in one module's tick is contained: it is caught, converted to
     `unavailable`, and the supervisor keeps ticking.

Subclasses implement `poll()` and either return data or raise Unavailable.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

log = logging.getLogger("tracerd.mod")

OK = "ok"
DEGRADED = "degraded"
UNAVAILABLE = "unavailable"
STARTING = "starting"


class Unavailable(Exception):
    """Raise from poll() when a dependency is absent.

    The message becomes the user-facing `reason`, so write it for a technician
    standing next to a vehicle: "no can0 interface", not "ENODEV".
    """


class Degraded(Exception):
    """Raise from poll() when working but with caveats. Carries data."""

    def __init__(self, reason: str, data: Any = None):
        super().__init__(reason)
        self.data = data


class Module:
    name = "base"
    interval = 1.0          # seconds between polls when healthy
    backoff_interval = 5.0  # slower when unavailable — don't spam a dead dep

    def __init__(self, hub):
        self.hub = hub
        self.state = STARTING
        self.reason: str | None = None
        self.since = time.time()
        self.data: Any = None
        # True while the module is doing something slow. Several modules reach
        # Headwaters over SSH, which can take seconds; without this the screen
        # is indistinguishable from a broken one, and the operator waits
        # without knowing whether anything is happening.
        self.busy = False
        self.busy_note = ""
        self.seq = 0
        self._task: asyncio.Task | None = None

    # ── lifecycle ────────────────────────────────────────────────────
    async def setup(self) -> None:
        """Optional one-time init. Failure here is not fatal."""

    async def poll(self) -> Any:
        """Return current data, or raise Unavailable / Degraded."""
        return None

    async def handle(self, op: str, args: dict) -> Any:
        raise Unavailable(f"{self.name} has no operation {op!r}")

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"mod:{self.name}")

    # How long a single module gets to finish cancelling before we give up on
    # it and move on. Short on purpose: this runs during system shutdown, and
    # anything a module still wants to do at this point is less important than
    # the unit actually stopping.
    STOP_TIMEOUT = 2.0

    async def stop(self) -> None:
        """Cancel the supervisor task, but never block forever waiting for it.

        `task.cancel()` only requests cancellation. A task parked in something
        that does not observe it — a shielded await, a thread executor, a child
        process being reaped — stays alive, and an unbounded `await self._task`
        then hangs the whole shutdown. That is exactly what happened on
        hardware: tracerd logged "shutting down", never returned from
        stop_all(), and systemd SIGKILLed it 90 s later while the operator
        stared at a black panel wondering if the power button worked.
        """
        if not self._task:
            return
        self._task.cancel()
        try:
            await asyncio.wait_for(self._task, timeout=self.STOP_TIMEOUT)
        except asyncio.CancelledError:
            # Normal: the task observed the cancellation and unwound.
            pass
        except asyncio.TimeoutError:
            # Abnormal but not fatal. Leave it; the process is exiting anyway,
            # and systemd's TimeoutStopSec is the final backstop.
            log.warning("module %s did not stop within %.1fs; abandoning it",
                        self.name, self.STOP_TIMEOUT)
        except Exception:
            log.exception("module %s raised while stopping", self.name)

    # ── the supervised loop ──────────────────────────────────────────
    async def _run(self) -> None:
        try:
            await self.setup()
        except Exception as exc:
            # Setup failure is a normal unavailable state, not a crash.
            self._transition(UNAVAILABLE, str(exc), None)

        while True:
            try:
                data = await self.poll()
                self._transition(OK, None, data)
            except Unavailable as exc:
                self._transition(UNAVAILABLE, str(exc), None)
            except Degraded as exc:
                self._transition(DEGRADED, str(exc), exc.data)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # An unexpected exception must not kill the module or the
                # daemon. Surface it as unavailable and keep going — but log
                # the traceback once, on the transition, so real bugs are
                # still findable.
                first = self.state != UNAVAILABLE
                self._transition(UNAVAILABLE, f"internal error: {exc}", None)
                if first:
                    log.exception("%s: unhandled error in poll()", self.name)

            await asyncio.sleep(
                self.interval if self.state in (OK, DEGRADED) else self.backoff_interval
            )

    def _transition(self, state: str, reason: str | None, data: Any) -> None:
        changed = (state != self.state) or (reason != self.reason)
        data_changed = data != self.data

        if changed:
            self.since = time.time()
            # ONE line per state change. Not per poll. See docs/api.md §3.
            if state == OK:
                log.info("%s: ok", self.name)
            elif state == DEGRADED:
                log.warning("%s: degraded — %s", self.name, reason)
            elif state == UNAVAILABLE:
                log.warning("%s: unavailable — %s", self.name, reason)

        self.state, self.reason, self.data = state, reason, data

        if changed or data_changed:
            self.publish()

    # ── wire format ──────────────────────────────────────────────────
    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "reason": self.reason,
            "since": round(self.since, 3),
            "busy": self.busy,
            "busy_note": self.busy_note,
            "data": self.data if self.state in (OK, DEGRADED) else None,
        }

    def set_busy(self, busy: bool, note: str = "") -> None:
        """Mark work in progress and publish straight away.

        Published immediately rather than on the next tick: the whole point is
        that the operator sees it the moment the work starts.
        """
        if self.busy == busy and self.busy_note == note:
            return
        self.busy, self.busy_note = busy, note
        self.publish()

    def publish(self) -> None:
        self.seq += 1
        self.hub.broadcast_snap(self.name, self.seq, self.snapshot())

    async def refresh(self) -> None:
        """Re-poll and publish immediately, without waiting for the next tick.

        Call this after an operation changes state. `publish()` alone is NOT
        enough: it serialises `self.data`, which only `poll()` updates. A
        handler that mutates internal state and then calls publish() will
        broadcast the PREVIOUS poll's data, so the change appears to the user
        only when the interval next elapses — 30 s for settings. That is
        exactly the "I changed the theme and nothing happened" bug.
        """
        try:
            data = await self.poll()
            self._transition(OK, None, data)
        except Unavailable as exc:
            self._transition(UNAVAILABLE, str(exc), None)
        except Degraded as exc:
            self._transition(DEGRADED, str(exc), exc.data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._transition(UNAVAILABLE, f"internal error: {exc}", None)

    # ── launcher tile ────────────────────────────────────────────────
    def tile_status(self) -> tuple[str, str]:
        """(text, colour) for this app's tile on the launcher.

        Default honours the copy convention: unknown values render as `--`.
        Subclasses override to show something meaningful.
        """
        if self.state == UNAVAILABLE:
            return "--", "#666"
        if self.state == STARTING:
            return "--", "#666"
        return "ok", "#74FE00"
