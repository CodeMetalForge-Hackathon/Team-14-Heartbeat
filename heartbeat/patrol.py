"""Sample-clock stop/settle/listen patrol with a separate rover I/O worker."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
import math
from pathlib import Path
import queue
import threading

import yaml

from .onset import CLIP_SAMPLES, PRE_SAMPLES, SAMPLE_RATE
from .rover import navigation_args


# These are protocol/state contracts, rather than tunable timing values.
_ACTIVE_DIALOG = {"PROMPTING", "PRE_LISTEN", "RESPONSE_WINDOW", "INTERVIEW"}
_STATUS_COLORS = {"NO_SIGNS": "led_no_signs", "POSSIBLE": "led_possible",
                  "CONFIRMED": "led_confirmed"}


def load_route(route):
    """Read and validate a list of named gaps before issuing any motor command."""
    if isinstance(route, (str, Path)):
        try:
            route = yaml.safe_load(Path(route).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(f"Cannot load patrol route: {exc}") from exc
    if not isinstance(route, list) or not route:
        raise ValueError("patrol route must be a nonempty list of gaps")
    result, names = [], set()
    for gap in route:
        if not isinstance(gap, Mapping) or set(gap) != {"name", "moves"}:
            raise ValueError("each gap requires only name and moves")
        name, moves = gap["name"], gap["moves"]
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("gap names must be nonempty and unique")
        if not isinstance(moves, list):
            raise ValueError(f"{name}: moves must be a list")
        checked = []
        for move in moves:
            if not isinstance(move, (list, tuple)) or len(move) != 2:
                raise ValueError(f"{name}: each move requires a command and value")
            command, value = move
            if command not in ("drive", "turn", "explore"):
                raise ValueError(f"{name}: unknown move {command!r}")
            limit = 600 if command == "explore" else (100 if command == "drive" else 180)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or abs(value) > limit or command == "explore" and value < 1):
                raise ValueError(f"{name}: {command} must be within +/-{limit}")
            checked.append((command, float(value)))
        names.add(name)
        result.append({"name": name, "moves": checked})
    return result


class Patrol:
    """Keep motors and dialog mutually exclusive without blocking audio hooks.

    ``engine`` should be the pipeline's :class:`DialogSession`, whose
    ``command_result`` runs reset/challenge and dispatches prompt actions on the
    processing thread. A pure engine is also accepted when its caller already
    owns the dialog/prompt hooks. All phase deadlines use ``pipeline.t``.

    Pause interrupts travel. Resume settles/listens at that stopped position;
    it never repeats an unknown fraction of an interrupted drive or turn.
    E-STOP is latched and a patrol cannot resume after E-STOP or abort.
    """

    def __init__(self, rover, pipeline, engine, prompter, route, cfg):
        self.rover, self.pipeline = rover, pipeline
        self.session = engine if hasattr(engine, "command_result") else None
        self._engine = None if self.session is not None else engine
        self.prompter = prompter
        self.route = load_route(route)
        self.cfg = dict(getattr(cfg, "rover", cfg))
        self.audio_stop = dict(self.cfg.get("audio_stop", {}))
        self.audio_enabled = self.audio_stop.get("enabled", False)
        if not isinstance(self.audio_enabled, bool):
            raise ValueError("rover.audio_stop.enabled must be boolean")
        self.trigger_mode = self.audio_stop.get("trigger_mode", "all")
        self.scheduled_listen = self.audio_stop.get("scheduled_listen", True)
        if self.trigger_mode not in {"all", "knocks"}:
            raise ValueError("rover.audio_stop.trigger_mode must be all or knocks")
        if not isinstance(self.scheduled_listen, bool):
            raise ValueError("rover.audio_stop.scheduled_listen must be boolean")
        if not self.scheduled_listen and not self.audio_enabled:
            raise ValueError("Disabling scheduled listening requires audio_stop.enabled")
        # Opt-in: after an audio stop settles, challenge immediately instead of
        # waiting for a second, fresh stationary cue during verification.
        self.challenge_after_stop = self.audio_stop.get("challenge_after_stop", False)
        if not isinstance(self.challenge_after_stop, bool):
            raise ValueError("rover.audio_stop.challenge_after_stop must be boolean")
        if self.audio_enabled:
            for key in ("verify_s", "max_age_s", "knock_window_s", "stop_timeout_s"):
                value = self.audio_stop.get(key)
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or value <= 0):
                    raise ValueError(f"rover.audio_stop.{key} must be positive and finite")
            count = self.audio_stop.get("knock_count")
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ValueError("rover.audio_stop.knock_count must be an integer >= 1")
        for key in ("settle_s", "gap_timeout_s", "speed_pct"):
            value = self.cfg[key]
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                raise ValueError(f"rover.{key} must be a finite nonnegative number")
        if self.cfg["gap_timeout_s"] == 0 or not 1 <= self.cfg["speed_pct"] <= 60:
            raise ValueError("patrol needs a positive timeout and speed no greater than 60")
        for key in _STATUS_COLORS.values():
            color = self.cfg[key]
            if (not isinstance(color, (list, tuple)) or len(color) != 3
                    or any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 255
                           for v in color)):
                raise ValueError(f"rover.{key} must contain three integer RGB channels")
        self.exploring = any(cmd == "explore" for gap in self.route for cmd, _ in gap["moves"])
        if self.exploring:
            if (len(self.route) != 1 or len(self.route[0]["moves"]) != 1
                    or not self.audio_enabled or self.scheduled_listen):
                raise ValueError("exploration needs one explore move and audio stops without scheduled listening")
            navigation_args(self.cfg["navigation"])
        self._explore_move = None
        self.results = []
        self.state = "IDLE"
        self.gap_index = -1
        self.battery = None
        self.done = False
        self._started = False
        self._closed = False
        self._halted = False
        self._generation = 0
        self._control_lock = threading.Lock()
        self._gate_started = None
        self._gap_started = 0.0
        self._move_index = 0
        self._deadline = None
        self._virtual_move = None
        self._noisy = False
        self._interrupted_move = False
        self._arrival_unverified = False
        self._pending_result = None
        self._paused_phase = None
        self._prompt_gate_until = 0.0
        self._phase_started = 0.0
        self._knocks = []
        self._trigger = None
        self._verified_cue = None
        self._verification_error = None
        self._fault_reason = None
        self._stop_thread = None
        self._jobs = queue.Queue()
        self._virtual = bool(getattr(rover, "virtual", False))
        self._worker_thread = None
        if not self._virtual:
            self._worker_thread = threading.Thread(
                target=self._worker, name="heartbeat-rover", daemon=True)
            self._worker_thread.start()
        gate = lambda: not self.motion_allowed
        if hasattr(rover, "set_motion_gate"):
            rover.set_motion_gate(gate)
        else:
            rover.engine_is_listening = gate
        pipeline.on_block.append(self._block)
        pipeline.on_end.append(self._end)
        self._unsubscribe = pipeline.bus.subscribe(self._event)

    def _event(self, event):
        if event.type == "prompt_start":
            self._prompt_gate_until = math.inf
            self._knocks.clear()
        elif event.type == "prompt_end" and self.engine is not None:
            self._prompt_gate_until = event.t + self.engine.gate_tail_s
        if not self.audio_enabled or self.done or self._halted:
            return
        if event.type == "error" and event.data.get("where") == "audio_monitor":
            if self.state == "VERIFYING":
                self._verification_error = str(event.data.get("message", "Audio monitor failed"))
            return
        if self.state not in {"MOVING", "VERIFYING"}:
            return
        cue = self._audio_cue(event)
        if cue is None:
            return
        if self.state == "MOVING":
            self._request_audio_stop(cue)
        elif self._verified_cue is None:
            self._verified_cue = cue
            self.pipeline.bus.publish("patrol_cue", self.pipeline.t, phase="verification", **cue)

    def _audio_cue(self, event):
        """Moving audio is a candidate only, never an answer to the dialog.

        Patrol gates all moving/verification knocks out of DialogEngine. We may
        inspect those gated candidates here, but only if their entire clip is
        newer than this phase and the last physical prompt's gate tail.
        """
        data = event.data
        if event.type == "audio_observation":
            if self.trigger_mode == "knocks":
                return None
            if not data.get("possible_distress"):
                return None
            if data.get("label") not in {"SCREAM", "SHOUT", "CRYING"} and not data.get("matched_phrases"):
                return None
            start, end = data.get("start"), event.t
            label = "HELP_PHRASE" if data.get("matched_phrases") else data["label"]
        elif event.type == "sound":
            # Passthrough accepts every bump; it must never drive this policy.
            if not data.get("is_knock") or data.get("classifier") in {None, "none"}:
                return None
            start = event.t - PRE_SAMPLES / SAMPLE_RATE
            end = start + CLIP_SAMPLES / SAMPLE_RATE
            label = "KNOCK_BURST"
        else:
            return None
        if (not isinstance(start, (int, float)) or not math.isfinite(start)
                or not math.isfinite(end) or end < start
                or start < max(self._phase_started, self._prompt_gate_until)
                or self.state == "VERIFYING" and end > self._deadline
                or not 0 <= self.pipeline.t - end <= self.audio_stop["max_age_s"]):
            return None
        if event.type == "sound":
            self._knocks = [t for t in self._knocks if event.t - self.audio_stop["knock_window_s"] <= t < event.t]
            self._knocks.append(event.t)
            if len(self._knocks) < self.audio_stop["knock_count"]:
                return None
            self._knocks.clear()
        return {"label": label, "start": start, "end": end, "score": data.get("score"),
                "transcript": data.get("transcript", ""),
                "matched_phrases": data.get("matched_phrases", [])}

    def _request_audio_stop(self, cue):
        # An operator STOP/pause can arrive between event qualification and
        # here. Never supersede its generation or reopen its motion interlock.
        with self._control_lock:
            if self._halted or self.done or self.state != "MOVING":
                return
            self._halted = True
            self._generation += 1
            generation = self._generation
        self._trigger = cue
        self._interrupted_move = self._arrival_unverified = True
        self._virtual_move = None
        self._close_gate()
        self._deadline = self.pipeline.t + self.audio_stop["stop_timeout_s"]
        self._emit_state("STOPPING")
        self.pipeline.bus.publish("patrol_cue", self.pipeline.t, phase="trigger", **cue)

        def stop():
            ok = self._stop_now()
            self.pipeline.call_soon(lambda: self._audio_stopped(generation, ok))

        if self._virtual:
            stop()
        else:
            # The normal worker may be waiting for DRIVE. STOP must bypass it.
            self._stop_thread = threading.Thread(target=stop, name="heartbeat-audio-stop", daemon=True)
            self._stop_thread.start()

    def _audio_stopped(self, generation, ok):
        with self._control_lock:
            if generation != self._generation or self.done or self.state != "STOPPING":
                return
            if ok:
                self._halted = False
        if not ok:
            self._audio_fault("STOP was not acknowledged; motion remains locked")
            return
        self._begin_settle()

    def _audio_fault(self, message):
        self._invalidate()
        self._fault_reason = message
        self._virtual_move = None
        self._close_gate()
        self._emit_state("FAULT")
        self._error(message)
        # Keep capture alive, but latch the motion interlock and retry E-STOP.
        threading.Thread(target=self._stop_now, args=(True,), daemon=True).start()

    def _begin_verify(self):
        self._engine_command("reset")
        self._verified_cue = self._verification_error = None
        self._deadline = self.pipeline.t + self.audio_stop["verify_s"]
        self._emit_state("VERIFYING")

    @property
    def engine(self):
        return self.session.engine if self.session is not None else self._engine

    @property
    def gap_name(self):
        return self.route[self.gap_index]["name"] if 0 <= self.gap_index < len(self.route) else None

    def _dialog_busy(self):
        engine = self.engine
        return engine is not None and (
            engine.state in _ACTIVE_DIALOG
            or bool(getattr(engine, "_awaiting_prompts", False))
            or getattr(engine, "_window", None) is not None
            or self.session is not None and self.session._active is not None)

    @property
    def motion_allowed(self):
        # Passive LISTENING is safe only while both detection and engine gates
        # are closed. An active challenge/window/interview is always unsafe.
        return (not self._halted and not self.done and self.state == "MOVING"
                and self._gate_started is not None and not self._dialog_busy())

    def snapshot(self):
        return {"state": self.state, "gap": self.gap_name, "index": self.gap_index,
                "moving": self.state == "MOVING", "settling": self.state == "SETTLING",
                "listening": self.state in {"LISTENING", "VERIFYING"},
                "paused": self.state in {"PAUSED", "REVIEW"},
                "audio_stop_enabled": self.audio_enabled,
                "trigger": deepcopy(self._trigger), "verified_cue": deepcopy(self._verified_cue),
                "fault": self._fault_reason,
                "estopped": self.state == "ESTOP" or bool(getattr(self.rover, "estopped", False)),
                "battery": self.battery, "done": self.done,
                "interrupted_move": self._interrupted_move,
                "arrival_unverified": self._arrival_unverified,
                "route": deepcopy(self.route),
                "results": deepcopy(self.results)}

    def approve(self, gap, operator):
        """Approve an already confirmed gap on the audio processing thread."""
        worker = getattr(self.pipeline, "_thread", None)
        if worker is None or worker.ident != threading.get_ident():
            return {"ok": False, "error": "gap approval requires the processing thread"}
        if not isinstance(gap, str) or not isinstance(operator, str) or not operator.strip():
            return {"ok": False, "error": "gap and nonempty operator initials are required"}
        result = next((item for item in self.results if item["gap"] == gap), None)
        if result is None or result["status"] != "CONFIRMED":
            return {"ok": False, "error": "only a recorded CONFIRMED gap can be approved"}
        operator = operator.strip()
        result.update(approved=True, approved_by=operator)
        self.pipeline.bus.publish("approval", self.pipeline.t,
                                  gap=gap, operator=operator, approved=True)
        self._emit_state(self.state)
        return {"ok": True, "patrol": self.snapshot()}

    def _emit_state(self, state):
        if state != self.state:
            self._phase_started = self.pipeline.t
            self._knocks.clear()
        self.state = state
        self.pipeline.bus.publish("patrol", self.pipeline.t, **self.snapshot())

    def _error(self, message):
        self.pipeline.bus.publish("error", self.pipeline.t, where="patrol", message=str(message))

    def start(self):
        if self._started:
            raise RuntimeError("a patrol can only start once")
        self._started = True
        self.pipeline.call_soon(self._start)
        return self

    def _start(self):
        if self._halted:
            return
        if self.engine is None:
            raise RuntimeError("initialize DialogSession before starting patrol")
        if self.engine.cfg.auto_challenge:
            self._error("patrol requires dialog.auto_challenge=False")
            self._halted = True
            self._finish("ABORTED")
            return
        if self._dialog_busy():
            self._error("patrol cannot move during an active dialog")
            self._halted = True
            self._finish("ABORTED")
            return
        self._next_gap()

    def _close_gate(self):
        if self._gate_started is None:
            self._gate_started = self.pipeline.t
            self.pipeline.detector.gate(self._gate_started, math.inf)
        if self.engine is not None:
            self.engine.gate_until(math.inf)

    def _open_gate(self):
        if self._gate_started is not None:
            self.pipeline.detector.gate(self._gate_started, self.pipeline.t)
            self._gate_started = None
        self.engine.gate_until(self.pipeline.t)

    def _engine_command(self, name):
        if self.session is not None:
            result = self.session.command_result(name)
            if not result["ok"]:
                raise RuntimeError(result.get("error", f"dialog rejected {name}"))
        else:
            actions = self.engine.command(name, self.pipeline.t)
            if actions:
                raise RuntimeError("use DialogSession to dispatch prompt actions")

    def _next_gap(self):
        self.gap_index += 1
        if self.gap_index == len(self.route):
            self._finish("DONE")
            return
        self._gap_started = self.pipeline.t
        self._move_index = 0
        self._noisy = False
        self._interrupted_move = False
        self._trigger = self._verified_cue = self._verification_error = None
        self._close_gate()
        self._emit_state("MOVING")
        self._next_move()

    def _next_move(self):
        if not self.motion_allowed:
            self._error("motion refused while dialog is active or detection gate is open")
            self._fail()
            return
        moves = self.route[self.gap_index]["moves"]
        if self._move_index >= len(moves):
            self._submit("stop", (), "stopped" if self.scheduled_listen else "checkpoint")
            return
        command, value = moves[self._move_index]
        self._move_index += 1
        if command == "explore":
            self._explore_move = (value, self.cfg["speed_pct"])
            self._submit("configure_navigation", (self.cfg["navigation"],), "navigation_ready")
        else:
            self._submit(command, (value, self.cfg["speed_pct"]), "moved")

    def _submit(self, method, args, completion=None):
        generation = self._generation
        if self._virtual:
            try:
                ok = getattr(self.rover, method)(*args)
            except Exception as exc:
                self._error(exc)
                ok = False
            if completion == "moved" and ok is True:
                duration = self.rover.duration(method, *args)
                self._virtual_move = (self.pipeline.t + duration, generation, completion)
            else:
                self._completed(generation, completion, ok)
        else:
            self._jobs.put((generation, method, args, completion))

    def _worker(self):
        while True:
            job = self._jobs.get()
            if job is None:
                return
            generation, method, args, completion = job
            if generation != self._generation or self._closed:
                continue
            if method in ("drive", "turn", "explore") and not self.motion_allowed:
                ok = False
            else:
                try:
                    ok = getattr(self.rover, method)(*args)
                except Exception as exc:
                    self._error(exc)
                    ok = False
            self.pipeline.call_soon(
                lambda g=generation, c=completion, value=ok: self._completed(g, c, value))

    def _completed(self, generation, completion, ok):
        if generation != self._generation or self.done or self._halted:
            return
        if completion == "reported":
            if ok is not True:
                self._error("LED update failed")
            if self.audio_enabled:
                self._emit_state("REVIEW")
            else:
                self._next_gap()
        elif completion == "status":
            if isinstance(ok, dict):
                self.battery = ok.get("batt", ok.get("battery"))
                self._emit_state(self.state)
        elif ok is not True and completion in ("moved", "stopped", "checkpoint", "navigation_ready"):
            self._error("rover operation failed; patrol stopped")
            self._fail()
        elif completion == "navigation_ready":
            if self.motion_allowed:
                self._submit("explore", self._explore_move, "moved")
        elif completion == "moved":
            self._next_move()
        elif completion == "stopped":
            self._begin_settle()
        elif completion == "checkpoint":
            # Queue advancement rather than recursively walking empty gaps.
            # A candidate or operator STOP can invalidate this callback first.
            self.pipeline.call_soon(lambda: self._advance_checkpoint(generation))

    def _advance_checkpoint(self, generation):
        if generation == self._generation and self.motion_allowed:
            self._next_gap()

    def _begin_settle(self):
        self._deadline = self.pipeline.t + self.cfg["settle_s"]
        self._emit_state("SETTLING")
        self._submit("status", (), "status")

    def _begin_listen(self):
        # reset keeps BackgroundRate history; never replace the engine here.
        self._open_gate()
        if getattr(self.prompter, "virtual", False) and "sim_survivor_gap" in self.cfg:
            self.prompter.no_survivor = self.gap_name != self.cfg["sim_survivor_gap"]
        self._engine_command("reset")
        self._deadline = self.pipeline.t + self.cfg["gap_timeout_s"]
        self._emit_state("LISTENING")
        self._engine_command("challenge")

    def _block(self, t):
        if self.done or not self._started or self.state == "IDLE":
            return
        if self._gate_started is not None:
            self.engine.gate_until(math.inf)
        if self.state == "STOPPING" and t >= self._deadline:
            self._audio_fault("STOP acknowledgement timed out; motion remains locked")
        elif self.state == "MOVING":
            if self._dialog_busy():
                self._error("active dialog during motion; emergency stop requested")
                self._fail()
                return
            if self._virtual_move is not None and t >= self._virtual_move[0]:
                _, generation, completion = self._virtual_move
                self._virtual_move = None
                self.rover.advance(t)
                self._completed(generation, completion, True)
        elif (self.state == "SETTLING" and t >= max(self._deadline, self._prompt_gate_until)
              and not self._dialog_busy()):
            if self.audio_enabled and not self.challenge_after_stop:
                self._begin_verify()
            else:
                self._begin_listen()
        elif self.state == "VERIFYING" and t >= self._deadline:
            if self._verified_cue is not None:
                self._begin_listen()
            else:
                self._gap_result(self.engine.snapshot(), "AUDIO_ERROR" if self._verification_error else "UNCONFIRMED")
        elif self.state == "LISTENING":
            snap = self.engine.snapshot()
            self._noisy = self._noisy or snap.get("noisy", False) or snap["state"] == "TOO_NOISY"
            if snap["state"] == "NO_RESPONSE" and not self._dialog_busy():
                self._gap_result(snap, "TOO_NOISY" if self._noisy else "NO_RESPONSE")
            elif snap["state"] == "CONFIRMED" and not self._dialog_busy():
                self._gap_result(snap, "CONFIRMED")
            elif t >= self._deadline:
                self._close_gate()
                self._pending_result = (snap, "TIMEOUT")
                self._engine_command("reset")
                self._emit_state("DRAINING")
        elif self.state == "DRAINING" and not self._dialog_busy():
            snap, reason = self._pending_result
            self._pending_result = None
            self._gap_result(snap, reason)

    def _gap_result(self, snap, reason):
        self._close_gate()
        result = {"gap": self.gap_name, "status": snap["status"],
                  "odds": snap["odds"], "combined_p": snap["combined_p"],
                  "answers": dict(snap["answers"]),
                  "duration": self.pipeline.t - self._gap_started,
                  "noisy": bool(self._noisy), "reason": reason,
                  "interrupted_move": self._interrupted_move,
                  "arrival_unverified": self._arrival_unverified}
        if self.audio_enabled:
            result.update(trigger=deepcopy(self._trigger), verified_cue=deepcopy(self._verified_cue),
                          verification_error=self._verification_error)
            if self._trigger or self._verified_cue:
                # A missing answer never erases the initial possible-person cue.
                if result["status"] != "CONFIRMED":
                    result["status"] = "POSSIBLE"
        self.results.append(result)
        self.pipeline.bus.publish("gap_result", self.pipeline.t, **result)
        self._emit_state("REPORTING")
        self._submit("led", tuple(self.cfg[_STATUS_COLORS[result["status"]]]), "reported")

    def _finish(self, state):
        self._close_gate()
        self.done = True
        self._emit_state(state)
        self.pipeline.bus.publish("patrol_done", self.pipeline.t, state=state,
                                  aborted=state != "DONE", gaps=deepcopy(self.results))
        if self.cfg.get("stop_on_done", True):
            self.pipeline.stop()

    def _fail(self):
        self._invalidate()
        self._virtual_move = None
        state = "ESTOP" if getattr(self.rover, "estopped", False) else "ABORTED"
        # I/O belongs on an independent thread, never inside a sample hook.
        threading.Thread(target=self._stop_now, args=(True,), daemon=True).start()
        self._finish(state)

    def _invalidate(self):
        with self._control_lock:
            self._halted = True
            self._generation += 1
            return self._generation

    def _stop_now(self, emergency=False):
        try:
            stop = getattr(self.rover, "estop", self.rover.stop) if emergency else self.rover.stop
            return stop() is True
        except Exception as exc:
            self._error(f"STOP failed: {exc}")
            return False

    def command(self, name):
        """Operator controls. STOP is dispatched before queuing any state change.

        Call from the keyboard/UI control thread; real serial replies may block.
        The emergency stop method bypasses the ordinary rover move worker.
        """
        name = str(name).lower()
        if self._fault_reason and name in {"pause", "resume"}:
            return {"ok": False, "error": "motion fault is latched; restart after checking the rover"}
        if name == "resume":
            if self.done or self.state not in {"PAUSED", "REVIEW"} or getattr(self.rover, "estopped", False):
                return {"ok": False, "error": "resume requires a paused patrol"}
            generation = self._generation
            self.pipeline.call_soon(lambda: self._resume(generation))
            return {"ok": True}
        if name not in ("pause", "abort", "estop", "stop"):
            return {"ok": False, "error": "unknown patrol command"}
        generation = self._invalidate()
        ok = self._stop_now(name != "pause")
        self.pipeline.call_soon(lambda: self._control(name, generation, ok))
        return {"ok": ok}

    def _control(self, name, generation, stopped=True):
        if self.done or generation != self._generation:
            return
        if not stopped and name == "pause":
            self._audio_fault("Pause STOP was not acknowledged; motion remains locked")
            return
        if name == "pause" and getattr(self.rover, "estopped", False):
            name = "estop"
        self._interrupted_move = self._interrupted_move or self.state == "MOVING"
        self._arrival_unverified = self._arrival_unverified or self._interrupted_move
        self._virtual_move = None
        self._close_gate()
        self.pipeline.bus.publish("operator", self.pipeline.t, command=name, source="patrol")
        self._engine_command("reset")
        if name == "pause":
            if self.state != "PAUSED":
                self._paused_phase = self.state
            self._emit_state("PAUSED")
        else:
            self._finish("ESTOP" if name in ("stop", "estop") else "ABORTED")

    def _resume(self, generation):
        with self._control_lock:
            if (generation != self._generation or self.done or self.state not in {"PAUSED", "REVIEW"}
                    or getattr(self.rover, "estopped", False)):
                return
            reviewed = self.state == "REVIEW" or self._paused_phase == "REVIEW"
            self._halted = False
        self.pipeline.bus.publish("operator", self.pipeline.t, command="resume", source="patrol")
        if reviewed:
            # The result is tied to the actual stopped position. Never replay
            # the remainder of an interrupted move with an unknown distance.
            self._paused_phase = None
            if self.exploring:
                self.route.append({"name": f"Explore {len(self.route) + 1}",
                                   "moves": list(self.route[0]["moves"])})
            self._next_gap()
            return
        if self.gap_index < 0:
            self._next_gap()
            return
        if self._paused_phase == "REPORTING":
            self._emit_state("REPORTING")
            result = self.results[-1]
            self._submit("led", tuple(self.cfg[_STATUS_COLORS[result["status"]]]), "reported")
            return
        self._pending_result = None
        # If a physical prompt is finishing, wait before starting a new one.
        self._deadline = self.pipeline.t + self.cfg["settle_s"]
        self._emit_state("SETTLING")

    def _end(self, t):
        if self._started and not self.done:
            self._invalidate()
            threading.Thread(target=self._stop_now, daemon=True).start()
            self._finish("ABORTED")

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._invalidate()
        self._stop_now()
        self._jobs.put(None)
        if self._worker_thread is not None and self._worker_thread is not threading.current_thread():
            self._worker_thread.join()
        self._unsubscribe()


def prepare_patrol_simulation(profile, route, *, bus=None, run_dir=None, argv=None, seed=1,
                              realtime=False):
    """Build a hardware-free patrol with a simulated survivor at Gap 2 only."""
    from .prompter import FakePrompter
    from .dialog import PromptKnocks, PromptVoice
    from .rover import FakeRover
    from .runner import SimulationSource, prepare_runtime

    route = load_route(route)
    profile = replace(profile, dialog=dict(profile.dialog, auto_challenge=False, auto_interview=True))
    cfg = profile.rover
    patrol = None

    class GapPrompter(FakePrompter):
        def respond(self, batch, t_end):
            # Canceled prompts must not plant answers in a later gap.
            if patrol is None or patrol.state != "LISTENING":
                return []
            return super().respond(batch, t_end)

    planner = FakePrompter(cfg=profile.prompter)
    drain_budget = sum(planner.duration(PromptVoice(key)) for key in
                       ["copy_knocks", "we_hear_you", *profile.dialog["questions"]])
    drain_budget += planner.duration(PromptKnocks(max(profile.dialog["patterns"]),
                                                profile.dialog["interval_s"]))
    # A timed-out gap still lets its physical/virtual prompt finish before moving.
    travel = sum(value if command == "explore" else abs(value) / (cfg["fake_speed_cm_s"] if command == "drive"
                             else cfg["fake_turn_deg_s"])
                 * cfg["default_speed_pct"] / cfg["speed_pct"]
                 for gap in route for command, value in gap["moves"])
    duration = len(route) * (cfg["gap_timeout_s"] + cfg["settle_s"] + drain_budget) + travel
    if not cfg.get("stop_on_done", True):
        duration = max(duration, profile.server["dashboard_sim_duration_s"])
    runtime = prepare_runtime(
        profile,
        lambda: SimulationSource(profile, duration_s=duration, seed=seed, no_survivor=True,
                                 realtime=realtime),
        lambda pipeline: GapPrompter(pipeline.bus, cfg=profile.prompter,
                                     clock=lambda: pipeline.t, no_survivor=True, seed=seed),
        bus=bus, run_dir=run_dir, argv=argv, rng_seed=seed, collect_events=True)
    rover = FakeRover(cfg=cfg, bus=runtime.bus, clock=lambda: runtime.pipeline.t)
    patrol = Patrol(rover, runtime.pipeline, runtime.session, runtime.session.prompter, route, cfg)
    if cfg.get("sim_audio_cue", False):
        injected = set()

        def demo_audio(t):
            # Explicit synthetic labels: exercise control flow without loading
            # CLAP, opening a microphone, or pretending to measure accuracy.
            phase = patrol.state
            delay = cfg["sim_audio_cue_after_s"]
            if phase == "VERIFYING":
                delay = cfg["audio_stop"]["demo_window_s"]
            if (phase in {"MOVING", "VERIFYING"} and phase not in injected
                    and t >= patrol._phase_started + delay):
                injected.add(phase)
                runtime.bus.publish("audio_observation", t, start=patrol._phase_started,
                                    label="SCREAM", score=1.0, possible_distress=True,
                                    matched_phrases=[], transcript="", alert=True, simulated=True)

        runtime.pipeline.on_block.append(demo_audio)
    runtime.on_close.extend([patrol.close, rover.close])
    patrol.start()
    return runtime, patrol
