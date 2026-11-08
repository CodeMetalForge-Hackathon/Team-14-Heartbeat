"""The audio processing loop, with hooks for the future dialog engine."""

from __future__ import annotations

import threading
import queue
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
import heapq
import math
from pathlib import Path
import time
from typing import Any
from concurrent.futures import ThreadPoolExecutor


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
        self.on_audio: list[Callable[[Any], Any]] = []
        self.on_sound: list[Callable[[Any], Any]] = []
        self.on_end: list[Callable[[float], Any]] = []
        self.t = 0.0
        self._stop = threading.Event()
        self._finished = threading.Event()
        self._thread: threading.Thread | None = None
        self._close_lock = threading.Lock()
        self._closed = False
        self._messages: queue.Queue[Callable[[], Any]] = queue.Queue()
        # Offline files/simulations remain synchronous and deterministic. Live
        # CLAP requests run independently of the source/detector processing loop.
        from .audio import WavSource
        offline = ((isinstance(source, WavSource) and not source.realtime)
                   or (isinstance(source, SimulationSource) and not source._realtime))
        self._async_inference = bool(getattr(classifier, "async_inference", False)) and not offline
        self._inference = None
        self._pending_inference = deque()
        self.inference_failed = False
        self._monitor = None
        monitor_cfg = getattr(classifier, "settings", {}).get("monitor")
        if monitor_cfg and monitor_cfg["enabled"]:
            from .audio_monitor import AudioMonitor
            self._monitor = AudioMonitor(classifier, bus, monitor_cfg, profile.prompter["gate_tail_s"])
            self.on_audio.append(self._monitor.feed)

    @property
    def detection_watermark(self):
        """Last stream time for which all possible classifications are known."""
        from .onset import CLIP_SAMPLES, PRE_SAMPLES, SAMPLE_RATE
        watermark = self.t - (CLIP_SAMPLES - PRE_SAMPLES) / SAMPLE_RATE
        if self._pending_inference:
            # Strictly before the oldest outstanding impact, so a knock exactly
            # on a window deadline is delivered before that window can close.
            watermark = min(watermark, self._pending_inference[0][0].t - 1 / SAMPLE_RATE)
        return max(0.0, watermark)

    def call_soon(self, fn: Callable[[], Any]) -> None:
        """Queue a mutation for the processing thread's next block boundary."""
        if not callable(fn):
            raise TypeError("call_soon requires a callable")
        self._messages.put(fn)

    def _drain_messages(self) -> None:
        while True:
            try:
                fn = self._messages.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception as exc:
                self._error("call_soon", exc)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def finished(self) -> bool:
        return self._finished.is_set()

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
        if getattr(self.classifier, "name", None) == "clap" and self._stop.is_set():
            return
        if self._async_inference:
            self._poll_inference()
            if self._stop.is_set():
                return
            if len(self._pending_inference) >= self.classifier.max_pending:
                self._fail_inference(RuntimeError("CLAP request queue is full; listening stopped"))
                return
            future = self._inference.submit(self.classifier.classify, onset)
            self._pending_inference.append((onset, future))
            return
        try:
            det = self.classifier.classify(onset)
        except Exception as exc:
            if getattr(self.classifier, "name", None) == "clap":
                self._fail_inference(exc)
                return
            self._error("classifier", exc)
            return
        self._publish_detection(det)

    def _publish_detection(self, det) -> None:
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
            evidence=getattr(det, "evidence", None),
            clip=None,
            _clip=det.clip,
        )
        self._call_hooks(self.on_sound, det, "on_sound")

    def _fail_inference(self, exc):
        if not self.inference_failed:
            self.inference_failed = True
            self._error("classifier", exc)
        for _, future in self._pending_inference:
            future.cancel()
        self._pending_inference.clear()
        self.stop()

    def _poll_inference(self, *, wait=False):
        while self._pending_inference and not self._stop.is_set():
            onset, future = self._pending_inference[0]
            # Live deadlines use sample time; request timeouts independently
            # bound network stalls, including at EOF when sample time stops.
            if self.t - onset.t > self.classifier.max_lag_s:
                self._fail_inference(RuntimeError("CLAP result exceeded maximum audio lag; listening stopped"))
                return
            if not wait and not future.done():
                return
            try:
                det = future.result(timeout=self.classifier.max_lag_s if wait else 0)
            except Exception as exc:
                self._fail_inference(exc)
                return
            self._pending_inference.popleft()
            if not self._stop.is_set():
                self._publish_detection(det)

    def _process(self) -> None:
        sample_rate = self.profile.audio.sample_rate
        level_hz = self.profile.audio.level_hz
        next_level = 0
        self.bus.publish("session_start", self.t)
        if self._async_inference:
            self._inference = ThreadPoolExecutor(max_workers=1, thread_name_prefix="heartbeat-classifier")
        try:
            for block in self.source:
                self._drain_messages()
                if self._stop.is_set():
                    break
                # Stay on the integer sample lattice rather than accumulating
                # rounding error by adding floating-point hop durations.
                self.t = (round(block.t0 * sample_rate) + len(block.x)) / sample_rate
                self._call_hooks(self.on_audio, block, "on_audio")
                try:
                    onsets = self.detector.process(block)
                except Exception as exc:
                    self._error("detector", exc)
                    onsets = []
                for onset in onsets:
                    self._sound(onset)
                self._poll_inference()
                if self.inference_failed:
                    break
                if self._monitor:
                    self._monitor.poll(self._inference, can_submit=not self._pending_inference)
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
                if self._async_inference:
                    self.inference_failed = True
                    self.stop()
        finally:
            self._drain_messages()
            try:
                for onset in self.detector.flush():
                    self._sound(onset)
            except Exception as exc:
                self._error("detector.flush", exc)
            self._poll_inference(wait=True)
            if self._monitor:
                if not self._stop.is_set():
                    self._monitor.poll(wait=True)
                self._monitor.close()
            if self._inference is not None:
                for _, future in self._pending_inference:
                    future.cancel()
                self._pending_inference.clear()
                self._inference.shutdown(wait=False, cancel_futures=True)
            self._call_hooks(self.on_end, self.t, "on_end")
            self._close_source()
            self.bus.publish("session_end", self.t)


@dataclass
class _PromptBatch:
    actions: list
    start: float
    generation: int
    number: int
    ready: threading.Event = field(default_factory=threading.Event)
    virtual_end: float | None = None


class DialogSession:
    """Wire the pure engine to one ordered prompt worker and sample-time gates."""

    def __init__(self, pipeline: Pipeline, prompter, *, rng_seed=1,
                 schedule=None, stop_when_finished=False, engine_factory=None):
        self.pipeline = pipeline
        self.prompter = prompter
        self.engine = None
        self._engine_factory = engine_factory
        self._seed = rng_seed
        self._schedule = schedule
        self._stop_when_finished = stop_when_finished
        self._virtual = bool(getattr(prompter, "virtual", False))
        self._tail = pipeline.profile.prompter["gate_tail_s"]
        self._generation = 0
        self._number = 0
        self._active: _PromptBatch | None = None
        self._waiting: deque[tuple[int, list]] = deque()
        self._jobs: queue.Queue[_PromptBatch | None] = queue.Queue()
        self._closing = threading.Event()
        self._ending = False
        self._worker_thread = threading.Thread(
            target=self._prompt_worker, name="heartbeat-prompts", daemon=True
        )
        self._worker_thread.start()
        pipeline.call_soon(self._initialize)
        pipeline.on_sound.append(self._sound)
        pipeline.on_block.append(self._block)
        pipeline.on_end.append(self._end)

    def _initialize(self) -> None:
        if self.engine is not None:
            return
        factory = self._engine_factory
        if factory is None:
            from .dialog import DialogEngine
            factory = DialogEngine
        self.engine = factory(
            self.pipeline.profile,
            lambda kind, t, data: self.pipeline.bus.publish(kind, t, **data),
            rng_seed=self._seed,
        )

    def command(self, name: str, **args) -> None:
        """Safe entry point for stdin, a server, or a later tool caller."""
        self.pipeline.call_soon(lambda: self._command(name, args))

    def _command(self, name: str, args: dict) -> None:
        self._execute_command(name, args)

    def command_result(self, name: str, **args) -> dict:
        """Execute on the processing thread and return a JSON-friendly result."""
        actions, errors = self._execute_command(name, args)
        result = {"ok": not errors, "actions": self._describe(actions), "state": self.engine.snapshot()}
        if errors:
            result["error"] = str(errors[0])
        return result

    def _execute_command(self, name: str, args: dict):
        self._initialize()
        errors = []
        processing_thread = threading.get_ident()

        def capture(event):
            if event.type == "error" and threading.get_ident() == processing_thread:
                errors.append(event.data.get("message", "command rejected"))

        unsubscribe = self.pipeline.bus.subscribe(capture)
        try:
            actions = self.engine.command(name, self.pipeline.t, **args)
        finally:
            unsubscribe()
        if name == "reset" and not errors:
            self._generation += 1
            self._waiting.clear()
        self._actions(actions)
        if self._active is not None:
            self.engine.gate_until(math.inf)
        return actions, errors

    def _sound(self, det) -> None:
        self._actions(self.engine.on_sound(det))

    def _block(self, t: float) -> None:
        batch = self._active
        if batch is not None and batch.virtual_end is not None and t >= batch.virtual_end:
            self._finish(batch, t)
        # The last 0.875 seconds may still contain onsets waiting for clips.
        # Deliver all detections before allowing a response window to expire.
        watermark = self.pipeline.detection_watermark
        self._actions(self.engine.tick(watermark))
        if self._stop_when_finished and self._active is None:
            snap = self.engine.snapshot()
            questions = self.pipeline.profile.dialog["questions"]
            if snap["state"] == "CONFIRMED" and all(q in snap["answers"] for q in questions):
                self.pipeline.stop()

    def _end(self, t: float) -> None:
        self._ending = True
        if self._active is not None:
            batch = self._active
            self.pipeline.detector.gate(batch.start, t + self._tail)
            self.engine.gate_until(t + self._tail)
            self.pipeline.bus.publish("prompt_end", t, batch=batch.number, interrupted=True)
            self._active = None
            self._generation += 1
        # EOF flush has now delivered every remaining onset, so the watermark
        # can reach the actual final sample without discarding late answers.
        if self.pipeline.inference_failed or (self.pipeline._async_inference and self.pipeline._stop.is_set()):
            # Missing classifications must not make an incomplete response
            # look like a valid shorter pattern when EOF closes the window.
            self.engine.command("reset", t)
        else:
            self._actions(self.engine.tick(t))
        snapshot = getattr(self.engine, "snapshot", None)
        if callable(snapshot):
            self.pipeline.bus.publish("state", t, **snapshot())

    @staticmethod
    def _describe(actions) -> list[dict]:
        return [dict(vars(action)) | {"action": type(action).__name__} for action in actions]

    def _actions(self, actions) -> None:
        if not actions or self._ending:
            return
        if self._active is not None:
            self._waiting.append((self._generation, list(actions)))
        else:
            self._start_batch(list(actions))

    def _start_batch(self, actions: list) -> None:
        self._number += 1
        batch = _PromptBatch(actions, self.pipeline.t, self._generation, self._number)
        self._active = batch
        self.pipeline.detector.gate(batch.start, math.inf)
        self.engine.gate_until(math.inf)
        self.pipeline.bus.publish(
            "prompt_start", batch.start, batch=batch.number, actions=self._describe(actions)
        )
        self._jobs.put(batch)
        if self._virtual:
            # Planning has no sleeps or hardware. Synchronize it before the
            # stream advances, so fast simulation never depends on CPU races.
            batch.ready.wait()

    def _prompt_worker(self) -> None:
        try:
            while True:
                batch = self._jobs.get()
                if batch is None:
                    return
                duration = 0.0
                try:
                    for action in batch.actions:
                        if self._closing.is_set():
                            break
                        self.prompter.run(action)
                        if self._virtual:
                            duration += self.prompter.duration(action)
                except Exception as exc:
                    self.pipeline.bus.publish(
                        "error", self.pipeline.t, where="prompter", message=str(exc)
                    )
                finally:
                    if self._virtual:
                        end = batch.start + duration
                        self.pipeline.call_soon(lambda b=batch, t=end: self._plan(b, t))
                    else:
                        end = self.pipeline.t
                        self.pipeline.call_soon(lambda b=batch, t=end: self._finish(b, t))
                    batch.ready.set()
        finally:
            try:
                self.prompter.close()
            except Exception as exc:
                self.pipeline.bus.publish(
                    "error", self.pipeline.t, where="prompter.close", message=str(exc)
                )

    def _plan(self, batch: _PromptBatch, t: float) -> None:
        if self._active is batch:
            batch.virtual_end = t

    def _finish(self, batch: _PromptBatch, t: float) -> None:
        if self._active is not batch:
            return
        self.pipeline.detector.gate(batch.start, t + self._tail)
        self.engine.gate_until(t + self._tail)
        self._active = None
        self.pipeline.bus.publish("prompt_end", t, batch=batch.number, interrupted=False)
        if self._virtual and self._schedule is not None:
            for event in self.prompter.respond(batch.actions, t):
                self._schedule(*event)
        if batch.generation == self._generation:
            self._actions(self.engine.on_prompts_finished(t))
        while self._active is None and self._waiting:
            generation, actions = self._waiting.popleft()
            if generation == self._generation:
                self._start_batch(actions)

    def close(self) -> None:
        self._closing.set()
        self._jobs.put(None)
        self._worker_thread.join()


class SimulationSource:
    """Chunked seeded audio, allowing responses to be scheduled as we stream."""

    def __init__(self, profile, *, duration_s, seed=1, no_survivor=False,
                 noise=False, realtime=False):
        import numpy as np

        self.profile = profile
        self.cfg = profile.prompter
        self.sample_rate = profile.audio.sample_rate
        self.block = profile.audio.block
        self._limit = round(duration_s * self.sample_rate)
        if not math.isfinite(duration_s) or self._limit <= 0:
            raise ValueError("simulation duration must contain at least one sample")
        if round(self.cfg["sim_chunk_s"] * self.sample_rate) < 1:
            raise ValueError("simulation background chunk must contain at least one sample")
        self._rng = np.random.default_rng(seed)
        self._events: list[tuple[int, int, Any]] = []
        self._active = []
        self._counter = 0
        self._closed = False
        self._background = np.empty(0, dtype=np.float32)
        self._realtime = realtime
        if not no_survivor:
            for t in self.cfg["sim_initial_knock_times_s"]:
                self.schedule(t, "knock", self.cfg["sim_initial_knock_gain_db"])
        if noise:
            t = self.cfg["sim_noise_first_s"]
            index = 0
            kinds = self.cfg["sim_noise_kinds"]
            while round(t * self.sample_rate) < self._limit:
                self.schedule(t, kinds[index % len(kinds)], self.cfg["sim_noise_gain_db"])
                t += self.cfg["sim_noise_interval_s"]
                index += 1

    def schedule(self, t: float, kind: str, gain_db: float) -> None:
        from . import synth

        sound = getattr(synth, kind)(rng=self._rng, cfg=self.profile.audio.synth)
        self._counter += 1
        heapq.heappush(self._events, (
            round(t * self.sample_rate), self._counter, sound * 10 ** (gain_db / 20)
        ))

    def _background_samples(self, n: int):
        import numpy as np
        from .synth import background

        pieces = []
        needed = n
        while needed:
            if not len(self._background):
                self._background = background(
                    self.cfg["sim_background_dbfs"], self.cfg["sim_chunk_s"],
                    rng=self._rng, cfg=self.profile.audio.synth,
                )
            count = min(needed, len(self._background))
            pieces.append(self._background[:count])
            self._background = self._background[count:]
            needed -= count
        return np.concatenate(pieces).copy()

    def __iter__(self):
        import numpy as np
        from .audio import AudioBlock

        origin = time.monotonic() if self._realtime else None
        for start in range(0, self._limit, self.block):
            if self._closed:
                return
            end = min(self._limit, start + self.block)
            audio = self._background_samples(end - start)
            while self._events and self._events[0][0] < end:
                self._active.append(heapq.heappop(self._events))
            remaining = []
            for sample, sequence, sound in self._active:
                a, b = max(start, sample), min(end, sample + len(sound))
                if b > a:
                    audio[a - start:b - start] += sound[a - sample:b - sample]
                if sample + len(sound) > end:
                    remaining.append((sample, sequence, sound))
            self._active = remaining
            if origin is not None:
                delay = start / self.sample_rate - (time.monotonic() - origin)
                if delay > 0:
                    time.sleep(delay)
            yield AudioBlock(start / self.sample_rate, np.clip(audio, -1, 1).astype(np.float32))

    def close(self) -> None:
        self._closed = True


@dataclass
class SimulationResult:
    snapshot: dict
    events: list
    t: float
    confirmed_t: float | None
    run_dir: Path | None = None


class SessionRuntime:
    """Own a sentinel's workers and finish its recording when audio ends.

    Starting is quick enough for an ASGI startup callback. A completion watcher
    drains prompt cleanup and the black box independently of the web event loop,
    allowing the dashboard to keep serving the finished session.
    """

    def __init__(self, pipeline, session, events, blackbox=None, unsubscribe=None, run_dir=None):
        self.pipeline = pipeline
        self.session = session
        self.bus = pipeline.bus
        self.profile = pipeline.profile
        self.events = events
        self.blackbox = blackbox
        self.run_dir = None if run_dir is None else Path(run_dir)
        self._unsubscribe = unsubscribe
        self.on_close: list[Callable[[], Any]] = []
        self._started = False
        self._closed = threading.Event()
        self._cleanup_lock = threading.Lock()

    def start(self):
        if self._started:
            return self
        self.pipeline.start()
        self._started = True
        threading.Thread(target=self._finish_when_done, name="heartbeat-session", daemon=True).start()
        return self

    def _finish_when_done(self):
        try:
            self.pipeline.join()
        finally:
            self._cleanup()

    def _cleanup(self):
        with self._cleanup_lock:
            if self._closed.is_set():
                return
            try:
                self.session.close()
            finally:
                try:
                    for callback in self.on_close:
                        try:
                            callback()
                        except Exception as exc:
                            self.bus.publish("error", self.pipeline.t,
                                             where="session.close", message=str(exc))
                    if self.blackbox is not None:
                        self.blackbox.close()
                finally:
                    if self._unsubscribe is not None:
                        self._unsubscribe()
                    self._closed.set()

    def close(self):
        if self._started:
            self.pipeline.stop()
            self.pipeline.join()
        else:
            self.pipeline.stop()
        self._cleanup()

    def run(self):
        try:
            self.start()
            self._closed.wait()
        except KeyboardInterrupt:
            self.close()
        return self.result()

    def result(self) -> SimulationResult:
        if self.session.engine is None:
            raise RuntimeError("session has not processed audio")
        confirmed = [event.t for event in self.events
                     if event.type == "state" and event.data.get("status") == "CONFIRMED"]
        return SimulationResult(self.session.engine.snapshot(), self.events, self.pipeline.t,
                                min(confirmed) if confirmed else None, self.run_dir)


def prepare_runtime(profile, source_factory, prompter_factory, *, bus=None,
                    run_dir=None, argv=None, rng_seed=1, stop_when_finished=False,
                    collect_events=False):
    """Attach recording before constructors can emit their first events."""
    from .classify import make_classifier
    from .events import EventBus
    from .onset import OnsetDetector

    bus = EventBus() if bus is None else bus
    blackbox = unsubscribe = source = prompter = None
    events = []
    try:
        if run_dir is not None:
            from .blackbox import BlackBox

            blackbox = BlackBox(run_dir, profile, [] if argv is None else argv, bus=bus)
            unsubscribe = bus.subscribe(blackbox)
        if collect_events:
            bus.subscribe(events.append)
        source = source_factory()
        pipeline = Pipeline(source, OnsetDetector(profile.onset), make_classifier(profile, bus=bus), bus, profile)
        if blackbox is not None:
            pipeline.on_audio.append(blackbox.on_audio)
        prompter = prompter_factory(pipeline)
        session = DialogSession(
            pipeline, prompter, rng_seed=rng_seed,
            schedule=getattr(source, "schedule", None), stop_when_finished=stop_when_finished,
        )
        return SessionRuntime(pipeline, session, events, blackbox, unsubscribe, run_dir)
    except Exception as exc:
        bus.publish("error", 0.0, where="session.setup", message=str(exc))
        for resource in (prompter, source, blackbox):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        if unsubscribe is not None:
            unsubscribe()
        raise


def prepare_simulation(profile=None, *, people=2, can_move="yes", hurt="yes",
                       no_survivor=False, noise=False, realtime=False, seed=1,
                       bus=None, duration_s=None, run_dir=None, argv=None,
                       stop_when_finished=None) -> SessionRuntime:
    from .config import load_profile
    from .prompter import FakePrompter

    profile = load_profile() if profile is None else profile
    profile = replace(profile, dialog=dict(profile.dialog, auto_challenge=True, auto_interview=True))
    if duration_s is None:
        key = "sim_no_survivor_duration_s" if no_survivor else "sim_duration_s"
        duration_s = profile.prompter[key]
    if stop_when_finished is None:
        stop_when_finished = not no_survivor
    return prepare_runtime(
        profile,
        lambda: SimulationSource(profile, duration_s=duration_s, seed=seed,
                                 no_survivor=no_survivor, noise=noise, realtime=realtime),
        lambda pipeline: FakePrompter(
            pipeline.bus, cfg=profile.prompter, clock=lambda: pipeline.t, people=people,
            can_move=can_move, hurt=hurt, no_survivor=no_survivor, seed=seed,
        ),
        bus=bus, run_dir=run_dir, argv=argv, rng_seed=seed,
        stop_when_finished=stop_when_finished, collect_events=True,
    )


def run_simulation(profile=None, *, people=2, can_move="yes", hurt="yes",
                   no_survivor=False, noise=False, realtime=False, seed=1,
                   bus=None, duration_s=None, run_dir=None, argv=None) -> SimulationResult:
    """Run headlessly; recording is opt-in here and enabled by default by CLI."""
    runtime = prepare_simulation(
        profile, people=people, can_move=can_move, hurt=hurt, no_survivor=no_survivor,
        noise=noise, realtime=realtime, seed=seed, bus=bus, duration_s=duration_s,
        run_dir=run_dir, argv=argv,
    )
    try:
        return runtime.run()
    finally:
        runtime.close()
