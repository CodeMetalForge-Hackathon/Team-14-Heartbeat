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
            if command not in ("drive", "turn"):
                raise ValueError(f"{name}: unknown move {command!r}")
            limit = 100 if command == "drive" else 180
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or abs(value) > limit):
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
        if event.type == "prompt_end" and self.engine is not None:
            self._prompt_gate_until = event.t + self.engine.gate_tail_s

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
                "listening": self.state == "LISTENING", "paused": self.state == "PAUSED",
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
            self._submit("stop", (), "stopped")
            return
        command, value = moves[self._move_index]
        self._move_index += 1
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
            if method in ("drive", "turn") and not self.motion_allowed:
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
            self._next_gap()
        elif completion == "status":
            if isinstance(ok, dict):
                self.battery = ok.get("batt", ok.get("battery"))
                self._emit_state(self.state)
        elif ok is not True and completion in ("moved", "stopped"):
            self._error("rover operation failed; patrol stopped")
            self._fail()
        elif completion == "moved":
            self._next_move()
        elif completion == "stopped":
            self._begin_settle()

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
        if self.state == "MOVING":
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
            self._begin_listen()
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
        if name == "resume":
            if self.done or self.state != "PAUSED" or getattr(self.rover, "estopped", False):
                return {"ok": False, "error": "resume requires a paused patrol"}
            generation = self._generation
            self.pipeline.call_soon(lambda: self._resume(generation))
            return {"ok": True}
        if name not in ("pause", "abort", "estop", "stop"):
            return {"ok": False, "error": "unknown patrol command"}
        generation = self._invalidate()
        ok = self._stop_now(name != "pause")
        self.pipeline.call_soon(lambda: self._control(name, generation))
        return {"ok": ok}

    def _control(self, name, generation):
        if self.done or generation != self._generation:
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
            if (generation != self._generation or self.done or self.state != "PAUSED"
                    or getattr(self.rover, "estopped", False)):
                return
            self._halted = False
        self.pipeline.bus.publish("operator", self.pipeline.t, command="resume", source="patrol")
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
    travel = sum(abs(value) / (cfg["fake_speed_cm_s"] if command == "drive"
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
    runtime.on_close.extend([patrol.close, rover.close])
    patrol.start()
    return runtime, patrol
