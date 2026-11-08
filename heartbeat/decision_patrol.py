"""Camera proposals behind the patrol's deterministic motion and rescue gates.

Only the observation worker touches the camera or Decisions service. A proposal
is never a motor command: the processing thread selects fixed profile arguments,
and the rover worker and firmware independently check permission at dispatch.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from copy import deepcopy
import math
import queue
import threading
from time import monotonic
from uuid import uuid4

from .camera import CameraError
from .decisions import DecisionsError
from .patrol import Patrol
from .rover import navigation_args


NAVIGATION_STATES = frozenset({"OBSERVING", "DECIDING", "HOLDING", "MOVING"})
STATIONARY_ACTIONS = ("HOLD", "STOP_AND_LISTEN")


def _finite_number(value):
    try:
        return (not isinstance(value, bool) and isinstance(value, (int, float))
                and math.isfinite(value))
    except OverflowError:
        return False


def _number(value, name, *, positive=True):
    if not _finite_number(value) or (value <= 0 if positive else value < 0):
        raise ValueError(f"rover.decisions.{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return value


def validate_decision_settings(settings):
    """Validate controller settings without starting threads or opening devices."""
    if not isinstance(settings, Mapping):
        raise ValueError("rover.decisions must be a mapping")
    cfg = deepcopy(dict(settings))
    cfg.setdefault("continuous_forward", False)
    cfg.setdefault("autonomous_exploration", False)
    cfg.setdefault("inspection_trigger", "audio_and_vision")
    cfg.setdefault("stop_image_history_size", cfg.get("history_size"))
    if not isinstance(cfg["continuous_forward"], bool):
        raise ValueError("rover.decisions.continuous_forward must be boolean")
    if not isinstance(cfg["autonomous_exploration"], bool):
        raise ValueError("rover.decisions.autonomous_exploration must be boolean")
    if (not isinstance(cfg["inspection_trigger"], str)
            or cfg["inspection_trigger"] not in {"audio_and_vision", "debris_only"}):
        raise ValueError("inspection_trigger must be audio_and_vision or debris_only")
    if cfg["inspection_trigger"] == "debris_only" and not cfg["autonomous_exploration"]:
        raise ValueError("debris_only requires autonomous_exploration")
    positive = ("session_s", "observation_max_age_s", "decision_interval_s", "hold_s",
                "forward_cm", "turn_deg", "speed_pct", "worker_join_s")
    try:
        for key in positive:
            _number(cfg[key], key)
        if cfg.get("forward_budget_cm") is not None:
            _number(cfg["forward_budget_cm"], "forward_budget_cm")
        if cfg["continuous_forward"]:
            if cfg.get("forward_budget_cm") is None or cfg["forward_budget_cm"] > 100:
                raise ValueError("Continuous mode requires a forward_budget_cm at most 100")
            duration = cfg.get("cruise_max_ms")
            if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= 30000:
                raise ValueError("cruise_max_ms must be an integer from 1 through 30000")
        if cfg.get("cruise_chunk_cm") is not None:
            chunk = _number(cfg["cruise_chunk_cm"], "cruise_chunk_cm")
            if chunk > 100:
                raise ValueError("cruise_chunk_cm must be at most 100")
        if cfg.get("min_battery_v") is not None:
            _number(cfg["min_battery_v"], "min_battery_v")
        if cfg["session_s"] > 600:
            raise ValueError("rover.decisions.session_s must be at most 600 seconds")
        _number(cfg["action_settle_s"], "action_settle_s", positive=False)
        confidence = _number(cfg["min_confidence"], "min_confidence")
        if confidence > 1:
            raise ValueError("rover.decisions.min_confidence must be at most 1")
        if not 0 < cfg["forward_cm"] <= 100 or not 0 < cfg["turn_deg"] <= 180:
            raise ValueError("rover.decisions action distances exceed the guarded protocol")
        if not 1 <= cfg["speed_pct"] <= 60:
            raise ValueError("rover.decisions.speed_pct must be between 1 and 60")
        for key in ("max_action_ms", "history_size", "stop_image_history_size"):
            if isinstance(cfg[key], bool) or not isinstance(cfg[key], int) or cfg[key] < 1:
                raise ValueError(f"rover.decisions.{key} must be a positive integer")
        if cfg["max_action_ms"] > 2000:
            raise ValueError("rover.decisions.max_action_ms exceeds the 2000 ms firmware ceiling")
        if not isinstance(cfg["positive_turn_is_right"], bool):
            raise ValueError("rover.decisions.positive_turn_is_right must be boolean")
        for key in ("max_response_bytes", "max_frame_bytes"):
            if isinstance(cfg[key], bool) or not isinstance(cfg[key], int) or cfg[key] < 1:
                raise ValueError(f"rover.decisions.{key} must be a positive integer")
        _number(cfg["read_timeout_s"], "read_timeout_s")
        tolerance = _number(cfg["probability_sum_tolerance"], "probability_sum_tolerance", positive=False)
        if tolerance >= 1:
            raise ValueError("rover.decisions.probability_sum_tolerance must be less than 1")
        camera = cfg["camera"]
        if not isinstance(camera, Mapping):
            raise ValueError("rover.decisions.camera must be a mapping")
        _number(camera["frame_timeout_s"], "camera.frame_timeout_s")
    except KeyError as exc:
        raise ValueError(f"Missing rover.decisions.{exc.args[0]}") from exc
    return cfg


class _ObservationError(Exception):
    """Fixed, safe diagnostic text; upstream exception bodies are never logged."""


class DecisionsPatrol(Patrol):
    """Guarded steps, or bounded continuous forward travel with live observations.

    ``arm_motion=False`` still reads real camera/range/audio inputs and records
    proposals. It never dispatches STEP, PIVOT or CRUISE. The camera and client must
    implement bounded operations; a controller watchdog faults an overrun and
    discards its eventual result, and shutdown only joins workers for a bound.
    """

    def __init__(self, rover, pipeline, engine, prompter, cfg, *, client, camera,
                 arm_motion=False, allow_turns=False):
        cfg = deepcopy(dict(getattr(cfg, "rover", cfg)))
        decisions = validate_decision_settings(cfg.get("decisions"))
        if decisions["continuous_forward"] and allow_turns and not decisions["autonomous_exploration"]:
            raise ValueError("Continuous forward mode does not support turns")
        navigation_args(cfg.get("navigation"))
        if not isinstance(arm_motion, bool) or not isinstance(allow_turns, bool):
            raise ValueError("Decisions motion and turn opt-ins must be boolean")
        if getattr(rover, "virtual", False):
            raise ValueError("Decisions patrol requires live inputs; virtual rover mode is unsupported")
        if cfg.get("audio_stop", {}).get("enabled") is not True:
            raise ValueError("Decisions patrol requires rover.audio_stop.enabled=True")
        classifier = getattr(getattr(pipeline, "classifier", None), "name", None)
        if not isinstance(classifier, str) or not classifier.strip() or classifier.lower() == "none":
            raise ValueError("Decisions patrol requires a classified audio source; passthrough is unsafe")
        self.decision_cfg = decisions
        self.client, self.camera = client, camera
        self.armed, self.allow_turns = arm_motion, allow_turns
        self._observation_jobs = queue.Queue(maxsize=1)
        self._observation_active = threading.Event()
        self._observation_thread = None
        self._pending_observation = None
        self._request_counter = 0
        self._last_frame_sequence = -1
        self._last_frame_at = None
        self._last_frame = None
        self._stop_images_lock = threading.Lock()
        self._stop_images = deque(maxlen=decisions["stop_image_history_size"])
        self._image_stop_reason = None
        self._last_front_cm = None
        self._last_action = None
        self._confidence = None
        self._latency = None
        self._decision_reason = "not_started"
        self._steps = 0
        self._forward_commanded_cm = 0.0
        self._history = deque(maxlen=decisions["history_size"])
        self._motion_intent = None
        self._navigation_ready = False
        self._session_started_at = None
        self._budget_expired = False
        self._next_observation_at = 0.0
        self._stop_context = None
        self._stop_until = None
        self._verification_for_resume = False
        self._cruise_active = False
        self._last_motion_error = None
        self._last_stop_reason = None
        self._last_stop_message = None
        self._debris_inspection_active = False
        super().__init__(rover, pipeline, engine, prompter,
                         [{"name": "Decision search 1", "moves": []}], cfg)
        self._observation_thread = threading.Thread(
            target=self._observation_worker, name="heartbeat-decisions", daemon=True)
        self._observation_thread.start()

    def snapshot(self):
        snap = super().snapshot()
        age = None if self._last_frame_at is None else max(0.0, monotonic() - self._last_frame_at)
        snap["decisions"] = {
            "armed": self.armed, "allow_turns": self.allow_turns,
            "last_action": self._last_action, "confidence": self._confidence,
            "latency_s": self._latency, "reason": self._decision_reason,
            "observation_age_s": age, "front_cm": self._last_front_cm,
            "steps": self._steps, "pending": self._pending_observation is not None,
            "forward_commanded_cm": self._forward_commanded_cm,
            "forward_budget_cm": self.decision_cfg.get("forward_budget_cm"),
            "autonomous_exploration": self.decision_cfg["autonomous_exploration"],
            "range_valid": _finite_number(self._last_front_cm) and 2 <= self._last_front_cm <= 400,
            "status_message": self._status_message(),
            "stop_reason": self._last_stop_reason,
            "stop_message": self._last_stop_message,
            "inspection_trigger": self.decision_cfg["inspection_trigger"],
            "stop_images": self._stop_image_metadata(),
        }
        return snap

    def _stop_image_metadata(self):
        with self._stop_images_lock:
            return [dict(metadata) for metadata, _ in reversed(self._stop_images)]

    def stop_image(self, image_id):
        """Return a retained JPEG through a side channel, never the event bus."""
        with self._stop_images_lock:
            return next((jpeg for metadata, jpeg in self._stop_images
                         if metadata["id"] == image_id), None)

    def _remember_stop_image(self, reason):
        # Repeated HOLD observations are retries at one stopped position.
        # Preserve its original evidence instead of filling the gallery.
        if reason == self._image_stop_reason:
            return
        self._image_stop_reason = reason
        image_id = uuid4().hex
        frame = self._last_frame
        captured, sequence, jpeg = frame if frame is not None else (None, None, None)
        metadata = {
            "id": image_id, "stop_t": float(self.pipeline.t), "reason": reason,
            "message": self._reason_message(reason),
            "kind": "possible_debris" if reason == "debris_detected" else "scene",
            "image_url": f"/api/stop-images/{image_id}" if jpeg is not None else None,
            "frame_sequence": sequence,
            "frame_age_s": None if captured is None else max(0.0, monotonic() - captured),
        }
        with self._stop_images_lock:
            self._stop_images.append((metadata, jpeg))

    def _reason_message(self, reason):
        if reason == "classified_audio_cue":
            return ("Knocks detected; investigating the sound" if
                    (self._trigger or {}).get("label") == "KNOCK_BURST" else
                    "Possible distress sound detected; investigating")
        return {
            "debris_detected": "Camera flagged possible debris; inspecting and listening",
            "decisions_stop_and_listen": "Camera requested an inspection; listening for a response",
            "model_hold": "Camera requested HOLD; checking the scene again",
            "low_confidence": "Movement confidence below threshold; checking the scene again",
            "obstacle_recheck": "Obstacle detected; looking for another route",
            "turn_requires_fresh_observation": "Changing direction; checking the turn before moving",
            "range_recheck": "Distance reading unavailable; retrying the sensor",
            "waiting_for_range": "Distance reading unavailable; retrying the sensor",
            "low_battery": "Reported battery voltage below the configured minimum",
            "battery_unavailable": "Battery reading unavailable",
            "session_budget_expired": "Exploration time limit reached",
            "forward_budget_exhausted": "Forward distance budget reached",
            "motion_disarmed": "Observation mode; movement is disabled",
            "operator_resume_verification": "Operator resumed; checking fresh audio before exploring",
            "operator_resume_observation": "Operator resumed; checking camera and distance before exploring",
            "operator_pause": "Exploration paused by operator",
            "aborted": "Exploration stopped by operator",
            "estop": "Emergency stop latched",
            "done": "Exploration complete",
            "preparing": "Preparing rover and navigation",
            "initial_settling": "Allowing the rover to settle before observing",
            "action_settling": "Movement completed; checking the next direction",
            "capturing": "Checking camera and distance",
            "awaiting_decision": "Assessing the camera view and sensor readings",
            "fresh_observation_after_resume": "Checking a fresh observation before continuing",
        }.get(reason, str(reason).replace("_", " "))

    def _status_message(self):
        if self.state == "PAUSED":
            return "Stopped: " + self._reason_message("operator_pause")
        if self.state == "MOVING":
            if self._cruise_active:
                return "Exploring forward"
            return {"TURN_LEFT_SHORT": "Turning left to explore another direction",
                    "TURN_RIGHT_SHORT": "Turning right to explore another direction"}.get(
                        self._last_action, "Exploring forward")
        message = self._reason_message(self._decision_reason)
        if self.state == "STOPPING":
            return "Stopping: " + message
        if self.state in {"HOLDING", "REVIEW", "SETTLING", "VERIFYING", "LISTENING", "DONE", "ABORTED", "ESTOP"}:
            return "Stopped: " + message
        return message

    def _decision_event(self, stage, **data):
        metadata = {**self.snapshot()["decisions"], **data, "stage": stage}
        self.pipeline.bus.publish("decision", self.pipeline.t, **metadata)

    def _record_outcome(self, outcome):
        if self._history:
            self._history[-1]["outcome"] = outcome

    def _emit_state(self, state):
        if state == "MOVING":
            self._image_stop_reason = None
        # Freeze the decision frame once per stop transition. STOPPING's
        # acknowledgement must not create a second copy of the same stop.
        if (state == "STOPPING" and self.state != "STOPPING"
                or state == "HOLDING" and self._last_frame is not None
                and self.state not in {"STOPPING", "HOLDING"}
                or state in {"PAUSED", "ESTOP", "ABORTED", "FAULT", "DONE"}
                and self.state not in {state, "STOPPING"}):
            reason = {"PAUSED": "operator_pause", "ESTOP": "estop", "ABORTED": "aborted"}.get(
                state, self._decision_reason)
            self._remember_stop_image(reason)
        # A full onset clip is nearly a second. Resetting this boundary for
        # every short capture/action would permanently starve knock detection.
        if self.state in NAVIGATION_STATES and state in NAVIGATION_STATES:
            self.state = state
            self.pipeline.bus.publish("patrol", self.pipeline.t, **self.snapshot())
        else:
            super()._emit_state(state)

    @property
    def motion_allowed(self):
        intent = self._motion_intent
        if (not self.armed or intent is None or not super().motion_allowed
                or intent["generation"] != self._generation
                or self._session_started_at is None or self._budget_expired
                or monotonic() - self._session_started_at >= self.decision_cfg["session_s"]):
            return False
        age = monotonic() - intent["observed"]
        if not 0 <= age <= self.decision_cfg["observation_max_age_s"]:
            return False
        action, front = intent["action"], intent["front_cm"]
        if not self._battery_ready():
            return False
        if not _finite_number(front) or not 2 <= front <= 400:
            return False
        if action == "FORWARD_SHORT":
            return front >= self.cfg["navigation"]["clear_cm"]
        return (action in {"TURN_LEFT_SHORT", "TURN_RIGHT_SHORT"} and self.allow_turns
                and front >= self.cfg["navigation"]["turn_clearance_cm"])

    def _cancel_observation(self):
        pending = self._pending_observation
        if pending is not None:
            pending["cancel"].set()
        self._pending_observation = None
        self._motion_intent = None

    def _invalidate(self):
        with self._control_lock:
            self._halted = True
            self._generation += 1
            self._cancel_observation()
            self._cruise_active = False
            return self._generation

    def _start(self):
        self._session_started_at = monotonic()
        super()._start()

    def _new_search_gap(self):
        self._debris_inspection_active = False
        if self.gap_index >= 0:
            self.route.append({"name": f"Decision search {len(self.route) + 1}", "moves": []})
        self.gap_index += 1
        self._gap_started = self.pipeline.t
        self._noisy = self._interrupted_move = self._arrival_unverified = False
        self._trigger = self._verified_cue = self._verification_error = None
        self._pending_result = None

    def _next_gap(self):
        self._new_search_gap()
        self._close_gate()
        self._decision_reason = "preparing" if not self._navigation_ready else "fresh_observation"
        self._emit_state("OBSERVING")
        self._launch_observation("observe" if self._navigation_ready else "prepare")

    def _launch_observation(self, kind="observe"):
        with self._control_lock:
            if (self._halted or self.done or self.state not in NAVIGATION_STATES
                    or self._pending_observation is not None or self._observation_active.is_set()
                    or self._dialog_busy() or self._budget_expired):
                return
            self._request_counter += 1
            pending = {"id": self._request_counter, "generation": self._generation,
                       "kind": kind, "after": monotonic(), "cancel": threading.Event()}
            budget = (self.audio_stop["stop_timeout_s"] if kind == "prepare" else
                      self.decision_cfg["camera"]["frame_timeout_s"] + self.decision_cfg["observation_max_age_s"])
            pending["deadline"] = pending["after"] + budget
            self._pending_observation = pending
            self._observation_active.set()
            self._observation_jobs.put_nowait(pending)
        self._decision_reason = "preparing" if kind == "prepare" else "capturing"
        if not self._cruise_active:
            self._emit_state("OBSERVING")
        self._decision_event("requested")

    def _current_observation(self, pending):
        return (pending is self._pending_observation and not pending["cancel"].is_set()
                and pending["generation"] == self._generation and not self._closed
                and not self._halted and not self.done and self.state in NAVIGATION_STATES)

    def _battery_ready(self, voltage=None):
        minimum = self.decision_cfg.get("min_battery_v")
        voltage = self.battery if voltage is None else voltage
        return minimum is None or (_finite_number(voltage) and voltage >= minimum)

    def _allowed_actions(self, front_cm, battery=None):
        choices = (["HOLD"] if self.decision_cfg["inspection_trigger"] == "debris_only"
                   else list(STATIONARY_ACTIONS))
        if self.decision_cfg["autonomous_exploration"]:
            choices.append("INSPECT_DEBRIS")
        if not _finite_number(front_cm) or not 2 <= front_cm <= 400 or not self._battery_ready(battery):
            return choices
        if front_cm >= self.cfg["navigation"]["clear_cm"] and (self._forward_remaining() > 0 or self._cruise_active):
            choices.append("FORWARD_SHORT")
        if self.allow_turns and front_cm >= self.cfg["navigation"]["turn_clearance_cm"]:
            choices.extend(("TURN_LEFT_SHORT", "TURN_RIGHT_SHORT"))
        return choices

    def _forward_remaining(self):
        budget = self.decision_cfg.get("forward_budget_cm")
        return math.inf if budget is None else max(0.0, budget - self._forward_commanded_cm)

    def _submit(self, method, args, completion=None):
        if method in {"step", "cruise"}:
            distance = min(args[0], self._forward_remaining())
            if distance <= 0:
                self._request_navigation_stop("budget", "forward_budget_exhausted")
                return
            # Charge before dispatch. Canceled/failed steps are not refunded:
            # their partial physical travel is unknown. Resume keeps this total.
            self._forward_commanded_cm += distance
            args = (distance, *args[1:])
        super()._submit(method, args, completion)

    def _prepare_navigation(self, pending):
        if self._stop_now() is not True:
            raise _ObservationError("Initial STOP was not acknowledged")
        if pending["cancel"].is_set():
            return None
        capabilities = self.rover.capabilities()
        if not isinstance(capabilities, Mapping) or not isinstance(capabilities.get("guarded"), bool):
            raise _ObservationError("Guarded rover capabilities are unavailable")
        if self.armed and capabilities["guarded"] is not True:
            raise _ObservationError("Firmware does not support guarded STEP/PIVOT")
        if self.decision_cfg["continuous_forward"] and capabilities.get("cruise") is not True:
            raise _ObservationError("Continuous mode requires updated CRUISE firmware")
        if pending["cancel"].is_set():
            return None
        if self.rover.configure_navigation(self.cfg["navigation"]) is not True:
            raise _ObservationError("Navigation configuration was not acknowledged")
        return {"prepared": True}

    def _observe(self, pending):
        try:
            frame = self.camera.capture(after=pending["after"], cancel=pending["cancel"])
        except CameraError as exc:
            if pending["cancel"].is_set():
                return None
            raise _ObservationError(str(exc)) from None
        except Exception:
            if pending["cancel"].is_set():
                return None
            raise _ObservationError("Camera capture failed") from None
        if pending["cancel"].is_set():
            return None
        captured, sequence = getattr(frame, "captured_monotonic", None), getattr(frame, "sequence", None)
        jpeg = getattr(frame, "jpeg", None)
        if (not _finite_number(captured) or not pending["after"] <= captured <= monotonic()
                or isinstance(sequence, bool) or not isinstance(sequence, int)
                or sequence <= self._last_frame_sequence
                or not isinstance(jpeg, bytes) or not jpeg
                or len(jpeg) > self.decision_cfg["max_frame_bytes"]):
            raise _ObservationError("Camera returned a stale, out-of-order, or invalid frame")
        if monotonic() - captured > self.decision_cfg["observation_max_age_s"]:
            raise _ObservationError("Camera observation expired before the decision request")
        pending["deadline"] = min(pending["deadline"], captured + self.decision_cfg["observation_max_age_s"])
        # Capture first so a healthy camera cold start cannot age the range
        # before its first frame. Both input timestamps remain motion gates.
        range_at = monotonic()
        if self._cruise_active:
            reading = self.rover.cruise_observation()
            if (not isinstance(reading, Mapping) or not _finite_number(reading.get("at"))
                    or not 0 <= range_at - reading["at"] <= self.decision_cfg["observation_max_age_s"]):
                raise _ObservationError("Continuous range telemetry expired or is unavailable")
            range_at = reading["at"]
            status = {"moving": reading.get("moving")}
        else:
            reading = self.rover.range(self.cfg["navigation"]["sensor_port"])
            status = self.rover.status()
        range_valid = (isinstance(reading, Mapping) and reading.get("valid") is True
                       and _finite_number(reading.get("cm")) and 2 <= reading["cm"] <= 400)
        # An explicit no-echo reply is still a healthy serial transaction.
        # Exploration can inspect the image and retry while stopped, but no
        # movement choice is offered until a fresh distance becomes valid.
        no_echo = (isinstance(reading, Mapping) and reading.get("valid") is False
                   and reading.get("cm") == -1)
        if not range_valid and not (self.decision_cfg["autonomous_exploration"]
                                    and no_echo and not self._cruise_active):
            raise _ObservationError("Front range is invalid or unavailable")
        if pending["cancel"].is_set():
            return None
        if not isinstance(status, Mapping) or status.get("moving") is not self._cruise_active:
            raise _ObservationError("Stopped rover status could not be verified")
        front = float(reading["cm"]) if range_valid else None
        telemetry = {"front_cm": front, "range_valid": range_valid,
                     "moving": self._cruise_active, "armed": self.armed,
                     "autonomous_exploration": self.decision_cfg["autonomous_exploration"],
                     "inspection_trigger": self.decision_cfg["inspection_trigger"],
                     "continuous_forward": self.decision_cfg["continuous_forward"],
                     "continuous_target_in_progress": self._cruise_active,
                     "allow_turns": self.allow_turns, "steps": self._steps,
                     "forward_commanded_cm": self._forward_commanded_cm,
                     "forward_budget_cm": self.decision_cfg.get("forward_budget_cm"),
                     "clear_cm": self.cfg["navigation"]["clear_cm"],
                     "turn_clearance_cm": self.cfg["navigation"]["turn_clearance_cm"],
                     "history": deepcopy(list(self._history))}
        battery = status.get("batt", status.get("battery"))
        if _finite_number(battery):
            telemetry["battery"] = float(battery)
        if pending["cancel"].is_set():
            return None
        observed = min(captured, range_at)
        if monotonic() - observed > self.decision_cfg["observation_max_age_s"]:
            raise _ObservationError("Camera or range observation expired before the decision request")
        allowed = self._allowed_actions(front, telemetry.get("battery", self.battery))
        self.pipeline.call_soon(lambda: self._frame_ready(pending, captured, sequence, telemetry, jpeg))
        try:
            result = self.client.choose(frame, telemetry, allowed)
        except DecisionsError as exc:
            if pending["cancel"].is_set():
                return None
            raise _ObservationError(str(exc)) from None
        except Exception:
            if pending["cancel"].is_set():
                return None
            raise _ObservationError("Decisions request failed") from None
        if pending["cancel"].is_set():
            return None
        return {"result": result, "captured": captured, "observed": observed, "sequence": sequence,
                "telemetry": telemetry, "allowed": allowed}

    def _observation_worker(self):
        while not self._closed:
            pending = self._observation_jobs.get()
            if pending is None:
                return
            value, error = None, None
            try:
                if not pending["cancel"].is_set() and not self._closed:
                    value = (self._prepare_navigation(pending) if pending["kind"] == "prepare"
                             else self._observe(pending))
            except _ObservationError as exc:
                error = str(exc)
            except Exception:
                error = "Rover observation failed"
            finally:
                self._observation_active.clear()
            self.pipeline.call_soon(lambda p=pending, v=value, e=error: self._observation_ready(p, v, e))

    def _frame_ready(self, pending, captured, sequence, telemetry, jpeg):
        if not self._current_observation(pending):
            return
        if monotonic() - captured > self.decision_cfg["observation_max_age_s"]:
            self._review_failure("Camera observation expired while waiting for processing")
            return
        self._last_frame_at, self._last_frame_sequence = captured, sequence
        self._last_frame = (captured, sequence, jpeg)
        pending["deadline"] = min(pending["deadline"], captured + self.decision_cfg["observation_max_age_s"])
        self._last_front_cm = telemetry["front_cm"]
        if "battery" in telemetry:
            self.battery = telemetry["battery"]
        self._decision_reason = "awaiting_decision"
        if not self._cruise_active:
            self._emit_state("DECIDING")

    def _validated_result(self, result, allowed):
        action = getattr(result, "action", None)
        probabilities = getattr(result, "probabilities", None)
        latency = getattr(result, "latency_s", None)
        if action not in allowed or not isinstance(probabilities, Mapping) or set(probabilities) != set(allowed):
            raise _ObservationError("Decisions returned an invalid action distribution")
        if any(not _finite_number(p) or not 0 <= p <= 1 for p in probabilities.values()):
            raise _ObservationError("Decisions returned invalid probabilities")
        tolerance = self.decision_cfg["probability_sum_tolerance"]
        if abs(sum(probabilities.values()) - 1) > tolerance:
            raise _ObservationError("Decisions probabilities do not sum to one")
        if probabilities[action] != max(probabilities.values()):
            raise _ObservationError("Decisions action disagrees with its probabilities")
        if not _finite_number(latency) or latency < 0:
            raise _ObservationError("Decisions returned an invalid latency")
        return action, float(probabilities[action]), float(latency)

    def _observation_ready(self, pending, value, error):
        if not self._current_observation(pending):
            return
        self._pending_observation = None
        if error is not None or value is None:
            self._review_failure(error or "Observation did not complete")
            return
        if value.get("prepared"):
            self._navigation_ready = True
            self._decision_reason = "initial_settling"
            self._next_observation_at = monotonic() + self.cfg["settle_s"]
            self._emit_state("HOLDING")
            return
        captured = value["captured"]
        if (not 0 <= monotonic() - value["observed"] <= self.decision_cfg["observation_max_age_s"]
                or value["sequence"] != self._last_frame_sequence):
            self._review_failure("Decision response used an expired or out-of-order observation")
            return
        try:
            action, confidence, latency = self._validated_result(value["result"], value["allowed"])
        except _ObservationError as exc:
            self._review_failure(str(exc))
            return
        except Exception:
            self._review_failure("Decisions returned a malformed result")
            return
        self._last_action, self._confidence, self._latency = action, confidence, latency
        self._history.append({"action": action, "confidence": confidence, "outcome": "proposed"})
        self._decision_reason = "proposal"
        self._decision_event("proposed", allowed_actions=value["allowed"])
        if pending["generation"] != self._generation or self._halted or self.done:
            return
        if action == "HOLD":
            self._hold("model_hold", pending["after"])
        elif confidence < self.decision_cfg["min_confidence"]:
            self._hold("low_confidence", pending["after"])
        elif action in {"STOP_AND_LISTEN", "INSPECT_DEBRIS"}:
            self._debris_inspection_active = action == "INSPECT_DEBRIS"
            self._record_outcome("stop_and_listen")
            self._verification_for_resume = False
            self._request_navigation_stop("investigate", "debris_detected" if
                                          action == "INSPECT_DEBRIS" else "decisions_stop_and_listen")
        elif not self.armed:
            self._hold("motion_disarmed", pending["after"])
        elif self._budget_due():
            self._request_navigation_stop("budget", "session_budget_expired")
        elif self._cruise_active and action in {"TURN_LEFT_SHORT", "TURN_RIGHT_SHORT"}:
            # Stop first and discard this proposal. Turning requires another
            # camera/range observation after STOP is acknowledged.
            self._record_outcome("turn_requires_stop")
            self._request_navigation_stop("hold", "turn_requires_fresh_observation")
        else:
            self._motion_intent = {"action": action, "observed": value["observed"],
                                   "front_cm": value["telemetry"]["front_cm"],
                                   "generation": self._generation}
            if self._cruise_active:
                self._record_outcome("continued")
                self._decision_reason = "continuous_motion"
                self._next_observation_at = monotonic() + self.decision_cfg["decision_interval_s"]
                self._decision_event("continued")
                return
            self._decision_reason = "guarded_motion"
            self._record_outcome("dispatched")
            self._emit_state("MOVING")
            if not self.motion_allowed:
                self._review_failure("Movement gate rejected the current observation")
                return
            speed, max_ms = self.decision_cfg["speed_pct"], self.decision_cfg["max_action_ms"]
            if action == "FORWARD_SHORT":
                if self.decision_cfg["continuous_forward"]:
                    self._cruise_active = True
                    self._next_observation_at = monotonic() + self.decision_cfg["decision_interval_s"]
                    distance = min(self._forward_remaining(),
                                   self.decision_cfg.get("cruise_chunk_cm") or self._forward_remaining())
                    self._submit("cruise", (distance, speed,
                                 self.decision_cfg["cruise_max_ms"]), "decision_moved")
                else:
                    self._submit("step", (self.decision_cfg["forward_cm"], speed, max_ms), "decision_moved")
            else:
                right = action == "TURN_RIGHT_SHORT"
                sign = 1 if right == self.decision_cfg["positive_turn_is_right"] else -1
                self._submit("pivot", (sign * self.decision_cfg["turn_deg"], speed, max_ms), "decision_moved")

    def _hold(self, reason, request_started):
        if self._halted or self.done:
            return
        if self._cruise_active:
            self._record_outcome(reason)
            self._request_navigation_stop("hold", reason)
            return
        self._motion_intent = None
        self._record_outcome(reason)
        if reason == "model_hold" and not self._battery_ready():
            self._decision_reason = "low_battery" if _finite_number(self.battery) else "battery_unavailable"
        elif (reason == "model_hold" and self.decision_cfg["autonomous_exploration"]
                and self._last_front_cm is None):
            self._decision_reason = "waiting_for_range"
        else:
            self._decision_reason = reason
        self._next_observation_at = max(monotonic() + self.decision_cfg["hold_s"],
                                        request_started + self.decision_cfg["decision_interval_s"])
        self._emit_state("HOLDING")
        self._decision_event("held")

    def _worker(self):
        # Patrol's original worker only guards DRIVE/TURN/EXPLORE. Guard the
        # new commands here too; RoverLink repeats this gate before its write.
        while True:
            job = self._jobs.get()
            if job is None:
                return
            generation, method, args, completion = job
            if generation != self._generation or self._closed:
                continue
            if method in {"drive", "turn", "explore", "step", "pivot", "cruise"} and not self.motion_allowed:
                ok = False
            else:
                try:
                    if method in {"step", "pivot", "cruise"}:
                        self._last_motion_error = None
                    ok = getattr(self.rover, method)(*args)
                except Exception:
                    ok = False
            self.pipeline.call_soon(lambda g=generation, c=completion, v=ok: self._completed(g, c, v))

    def _completed(self, generation, completion, ok):
        if generation != self._generation or self.done or self._halted:
            return
        if completion == "decision_moved":
            if self._cruise_active:
                self._cruise_active = False
                self._cancel_observation()
            self._motion_intent = None
            if ok is not True:
                if (self.decision_cfg["autonomous_exploration"]
                        and self._last_motion_error in {"obstacle_too_close", "range_unknown"}):
                    self._record_outcome(self._last_motion_error)
                    # RoverLink retires the connection after a firmware ERR.
                    # Reopening can reset the MCU, losing its RAM-only NAVCFG.
                    self._navigation_ready = False
                    self._request_navigation_stop("hold", "obstacle_recheck" if
                        self._last_motion_error == "obstacle_too_close" else "range_recheck")
                    return
                self._record_outcome("failed")
                self._review_failure("Guarded motion failed, expired, or was not acknowledged")
                return
            self._record_outcome("completed")
            self._steps += 1
            self._decision_reason = "action_settling"
            self._next_observation_at = max(monotonic() + self.decision_cfg["action_settle_s"],
                                            self._last_frame_at + self.decision_cfg["decision_interval_s"])
            self._emit_state("HOLDING")
            self._decision_event("completed")
            if self._forward_remaining() <= 0:
                self._request_navigation_stop("budget", "forward_budget_exhausted")
        elif completion == "reported":
            super()._completed(generation, completion, ok)
            if self._budget_due() and self.state == "REVIEW":
                self._request_navigation_stop("budget", "session_budget_expired")
        else:
            super()._completed(generation, completion, ok)

    def _event(self, event):
        if (event.type == "error" and event.data.get("source") == "rover"
                and event.data.get("command") in {"STEP", "PIVOT", "CRUISE"}):
            self._last_motion_error = event.data.get("message")
        super()._event(event)
        if self.done or self._halted:
            return
        if event.type == "prompt_start" and self.state in NAVIGATION_STATES:
            self._verification_for_resume = True
            self._request_navigation_stop("resume", "prompt_interrupted_navigation")
            return
        if event.type == "error" and event.data.get("where") == "audio_monitor":
            if (self.state in NAVIGATION_STATES
                    and self.decision_cfg["inspection_trigger"] != "debris_only"):
                self._review_failure("Audio monitoring failed during decision navigation")
            return
        if self.state in NAVIGATION_STATES - {"MOVING"}:
            cue = self._audio_cue(event)
            if cue is not None:
                self._request_audio_stop(cue)

    def _audio_cue(self, event):
        if self.decision_cfg["inspection_trigger"] == "debris_only":
            # Keep recording/classification active. Only evidence collected
            # after the camera selected a debris site can enter its interview.
            if not self._debris_inspection_active or self.state not in {"VERIFYING", "LISTENING"}:
                return None
        return super()._audio_cue(event)

    def _request_audio_stop(self, cue):
        if self.decision_cfg["inspection_trigger"] == "debris_only":
            return
        if self._halted or self.done or self.state not in NAVIGATION_STATES:
            return
        self._trigger = cue
        self._verification_for_resume = False
        generation = self._generation
        self.pipeline.bus.publish("patrol_cue", self.pipeline.t, phase="trigger", **cue)
        self._request_navigation_stop("investigate", "classified_audio_cue", expected_generation=generation)

    def _request_navigation_stop(self, context, reason, *, expected_generation=None):
        with self._control_lock:
            if (self.done or self._closed or self.state == "FAULT"
                    or expected_generation is not None and expected_generation != self._generation
                    or self._halted
                    or getattr(self.rover, "estopped", False)):
                return
            was_moving = self.state == "MOVING"
            self._halted = True
            self._generation += 1
            generation = self._generation
        self._cancel_observation()
        self._cruise_active = False
        if was_moving and self._history and self._history[-1]["outcome"] == "dispatched":
            self._record_outcome("interrupted")
        self._interrupted_move = self._interrupted_move or was_moving
        self._arrival_unverified = self._arrival_unverified or was_moving
        self._stop_context, self._decision_reason = context, reason
        self._last_stop_reason = reason
        self._last_stop_message = self._reason_message(reason)
        self._close_gate()
        self._deadline = self.pipeline.t + self.audio_stop["stop_timeout_s"]
        self._stop_until = monotonic() + self.audio_stop["stop_timeout_s"]
        self._emit_state("STOPPING")
        self._decision_event("stopping")

        def stop():
            ok = self._stop_now()
            self.pipeline.call_soon(lambda: self._audio_stopped(generation, ok))

        self._stop_thread = threading.Thread(target=stop, name="heartbeat-decisions-stop", daemon=True)
        self._stop_thread.start()

    def _audio_stopped(self, generation, ok):
        with self._control_lock:
            if generation != self._generation or self.done or self.state != "STOPPING":
                return
            context = self._stop_context
            if ok is True:
                self._stop_context = self._stop_until = None
                self._halted = False
        if ok is not True:
            self._audio_fault("STOP was not acknowledged; motion remains locked")
            return
        if context == "review":
            self._emit_state("REVIEW")
        elif context == "hold":
            self._next_observation_at = monotonic() + self.decision_cfg["hold_s"]
            self._emit_state("HOLDING")
        elif context == "budget":
            self._finish("DONE")
        else:
            self._begin_settle()

    def _review_failure(self, reason):
        if (self.decision_cfg["autonomous_exploration"] and self._cruise_active and reason in {
                "Front range is invalid or unavailable",
                "Continuous range telemetry expired or is unavailable"}):
            self._request_navigation_stop("hold", "range_recheck")
            return
        generation = self._generation
        if self._history and self._history[-1]["outcome"] == "dispatched":
            self._record_outcome("failed")
        self._decision_reason = reason
        self._error(reason)
        self._request_navigation_stop("review", reason, expected_generation=generation)

    def _budget_due(self):
        if (self._session_started_at is not None
                and monotonic() - self._session_started_at >= self.decision_cfg["session_s"]):
            self._budget_expired = True
        if self._forward_remaining() <= 0 and self.state not in {"MOVING", "STOPPING"}:
            self._budget_expired = True
        return self._budget_expired

    def _block(self, t):
        if self.done or not self._started or self.state == "IDLE":
            return
        expired = self._budget_due()
        if self._halted and self.state in NAVIGATION_STATES:
            return
        if expired and self.state in NAVIGATION_STATES | {"PAUSED", "REVIEW"}:
            self._request_navigation_stop("budget", "session_budget_expired")
            return
        if self.state == "STOPPING" and self._stop_until is not None and monotonic() >= self._stop_until:
            self._audio_fault("STOP acknowledgement timed out; motion remains locked")
            return
        pending = self._pending_observation
        if pending is not None and monotonic() >= pending["deadline"]:
            self._review_failure("Observation worker exceeded its bounded deadline")
            return
        if self.state in NAVIGATION_STATES:
            if self._dialog_busy():
                self._verification_for_resume = True
                self._request_navigation_stop("resume", "dialog_interrupted_navigation")
            elif self._cruise_active:
                if not self.motion_allowed:
                    self._review_failure("Continuous movement observation expired")
                elif monotonic() >= self._next_observation_at:
                    self._launch_observation()
            elif self.state == "OBSERVING" and self._pending_observation is None:
                self._launch_observation("observe" if self._navigation_ready else "prepare")
            elif self.state == "HOLDING" and monotonic() >= self._next_observation_at:
                self._launch_observation("observe" if self._navigation_ready else "prepare")
            return
        if self.state == "VERIFYING" and t >= self._deadline and self._verification_for_resume:
            self._verification_for_resume = False
            if self._verified_cue is not None or self._verification_error:
                super()._block(t)
            elif expired:
                self._request_navigation_stop("budget", "session_budget_expired")
            else:
                self._decision_reason = "fresh_observation_after_resume"
                self._emit_state("OBSERVING")
                self._launch_observation("observe" if self._navigation_ready else "prepare")
            return
        super()._block(t)

    def _resume(self, generation):
        with self._control_lock:
            if (generation != self._generation or self.done or self.state not in {"PAUSED", "REVIEW"}
                    or self._fault_reason or getattr(self.rover, "estopped", False)):
                return
            reviewed = self.state == "REVIEW" or self._paused_phase == "REVIEW"
            self._halted = False
        if reviewed and self.results and self.results[-1]["gap"] == self.gap_name:
            self._new_search_gap()
        else:
            self._trigger = self._verified_cue = self._verification_error = None
        self._paused_phase = None
        self._pending_result = None
        self.pipeline.bus.publish("operator", self.pipeline.t, command="resume", source="patrol")
        if self.decision_cfg["inspection_trigger"] == "debris_only":
            self._debris_inspection_active = False
            self._engine_command("reset")
            self._verification_for_resume = False
            self._request_navigation_stop("hold", "operator_resume_observation", expected_generation=generation)
            return
        self._verification_for_resume = True
        self._request_navigation_stop("resume", "operator_resume_verification", expected_generation=generation)

    def _control(self, name, generation, stopped=True):
        if (generation == self._generation and self.state == "MOVING" and self._history
                and self._history[-1]["outcome"] == "dispatched"):
            self._record_outcome("interrupted")
        super()._control(name, generation, stopped)
        with self._control_lock:
            if generation == self._generation and self.state == "PAUSED":
                # The stable PAUSED state itself closes the motor gate. Keep
                # _halted for an operator STOP whose acknowledgement is pending.
                self._halted = False

    def command(self, name):
        if str(name).lower() == "resume" and self._observation_active.is_set():
            return {"ok": False, "error": "Previous observation is still cancelling; wait or restart after checking its worker"}
        return super().command(name)

    def _finish(self, state):
        if self._history and self._history[-1]["outcome"] == "dispatched":
            self._record_outcome("interrupted")
        self._cancel_observation()
        self._decision_reason = "session_budget_expired" if state == "DONE" and self._budget_expired else state.lower()
        if state == "DONE" and self._forward_remaining() <= 0:
            self._decision_reason = "forward_budget_exhausted"
        super()._finish(state)

    def close(self):
        """Cancel pending work and bound every join, even after an I/O failure."""
        if self._closed:
            return
        self._closed = True
        self._invalidate()
        # Close may race a stuck network or camera call. Neither cleanup nor a
        # serial STOP can make shutdown wait indefinitely on the caller thread.
        def cleanup(operation):
            try:
                operation()
            except Exception:
                pass

        closers = [threading.Thread(target=cleanup, args=(operation,), daemon=True)
                   for operation in (self._stop_now, self.camera.close, self.client.close)]
        for closer in closers:
            closer.start()
        self._jobs.put(None)
        try:
            self._observation_jobs.put_nowait(None)
        except queue.Full:
            pass
        until = monotonic() + self.decision_cfg["worker_join_s"]
        for worker in (*closers, self._worker_thread, self._observation_thread, self._stop_thread):
            if worker is not None and worker is not threading.current_thread():
                worker.join(max(0.0, until - monotonic()))
        self._unsubscribe()
