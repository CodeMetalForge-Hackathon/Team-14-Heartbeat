"""The audio processing loop, with hooks for the future dialog engine."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any


class Pipeline:
    """Consume a source on one worker thread using only its stream timestamps.

    ``on_block`` callbacks receive the end-of-block time. ``on_sound`` callbacks
    receive each classified detection. Hooks run on the processing thread.
    """

    def __init__(self, source, detector, classifier, bus, profile):
        self.source = source
        self.detector = detector
        self.classifier = classifier
        self.bus = bus
        self.profile = profile
        self.on_block: list[Callable[[float], Any]] = []
        self.on_sound: list[Callable[[Any], Any]] = []
        self.t = 0.0
        self._stop = threading.Event()
        self._finished = threading.Event()
        self._thread: threading.Thread | None = None
        self._close_lock = threading.Lock()
        self._closed = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> Pipeline:
        if self._thread is not None:
            raise RuntimeError("a pipeline can only be started once")
        self._thread = threading.Thread(
            target=self._worker, name="heartbeat-audio", daemon=True
        )
        self._thread.start()
        return self

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            if self._thread is threading.current_thread():
                raise RuntimeError("cannot join the current processing thread")
            # Python 3.11 can mark a live thread stopped if SIGINT interrupts
            # Thread.join(). Wait independently until all cleanup is complete.
            if self._finished.wait(timeout):
                self._thread.join()

    def _worker(self) -> None:
        try:
            self._process()
        finally:
            self._finished.set()

    def run(self) -> None:
        self.start()
        try:
            self.join()
        except KeyboardInterrupt:
            self.stop()
            self.join()

    def stop(self) -> None:
        """Request shutdown and unblock a live source; safe to call repeatedly."""
        self._stop.set()
        self._close_source()

    def _error(self, where: str, exc: Exception) -> None:
        self.bus.publish("error", self.t, where=where, message=str(exc))

    def _close_source(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self.source.close()
            except Exception as exc:
                self._error("source.close", exc)

    def _call_hooks(self, hooks: list, value: Any, name: str) -> None:
        for hook in tuple(hooks):
            try:
                hook(value)
            except Exception as exc:
                self._error(name, exc)

    def _sound(self, onset) -> None:
        try:
            det = self.classifier.classify(onset)
        except Exception as exc:
            self._error("classifier", exc)
            return
        self.bus.publish(
            "sound",
            float(det.t),
            snr_db=float(det.snr_db),
            peak=float(det.peak),
            gated=bool(det.gated),
            score=float(det.score),
            is_knock=bool(det.is_knock),
            classifier=str(det.classifier),
            top=det.top,
            clip=None,
            _clip=det.clip,
        )
        self._call_hooks(self.on_sound, det, "on_sound")

    def _process(self) -> None:
        sample_rate = self.profile.audio.sample_rate
        level_hz = self.profile.audio.level_hz
        next_level = 0
        self.bus.publish("session_start", self.t)
        try:
            for block in self.source:
                if self._stop.is_set():
                    break
                # Stay on the integer sample lattice rather than accumulating
                # rounding error by adding floating-point hop durations.
                self.t = (round(block.t0 * sample_rate) + len(block.x)) / sample_rate
                try:
                    onsets = self.detector.process(block)
                except Exception as exc:
                    self._error("detector", exc)
                    onsets = []
                for onset in onsets:
                    self._sound(onset)
                if self.t >= next_level / level_hz:
                    self.bus.publish(
                        "level",
                        self.t,
                        dbfs=float(self.detector.level_db),
                        floor_db=float(self.detector.floor_db),
                    )
                    # Advance by stream time, including a source's partial hop.
                    while next_level / level_hz <= self.t:
                        next_level += 1
                self._call_hooks(self.on_block, self.t, "on_block")
        except Exception as exc:
            if not self._stop.is_set():
                self._error("source", exc)
        finally:
            try:
                for onset in self.detector.flush():
                    self._sound(onset)
            except Exception as exc:
                self._error("detector.flush", exc)
            self._close_source()
            self.bus.publish("session_end", self.t)
