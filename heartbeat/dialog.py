"""Pure sample-clock challenge/response and knock interview state machine."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, fields
import math
import random
from typing import TypeAlias

from .stats import BackgroundRate, combine, format_odds, round_p


@dataclass(frozen=True)
class PromptVoice:
    key: str


@dataclass(frozen=True)
class PromptKnocks:
    n: int
    interval_s: float


Action: TypeAlias = PromptVoice | PromptKnocks


@dataclass(frozen=True)
class DialogConfig:
    rate_window_s: float
    prior_events: float
    prior_s: float
    lambda_floor: float
    burst_knocks: int
    burst_window_s: float
    quiet_s: float
    auto_challenge: bool
    first_pattern: int
    patterns: list[int]
    interval_s: float
    window_s: float
    early_close_s: float
    min_matched_rounds: int
    p_confirm: float
    max_rounds: int
    max_empty_windows: int
    cooldown_s: float
    auto_interview: bool
    question_window_s: float
    question_early_close_s: float
    questions: list[str]
    profile: str

    @classmethod
    def parse(cls, cfg):
        if isinstance(cfg, cls):
            raw = asdict(cfg)
        else:
            raw = getattr(cfg, "dialog", cfg)
        if not isinstance(raw, Mapping):
            raise ValueError("dialog configuration must be a mapping or DialogConfig")
        expected = {field.name for field in fields(cls)}
        unknown, missing = set(raw) - expected, expected - set(raw)
        if unknown:
            raise ValueError(f"Unknown dialog keys: {', '.join(sorted(map(str, unknown)))}")
        if missing:
            raise ValueError(f"Missing dialog keys: {', '.join(sorted(missing))}")
        result = cls(**deepcopy(dict(raw)))
        for name in ("rate_window_s", "prior_s", "burst_window_s", "interval_s", "window_s",
                     "early_close_s", "question_window_s", "question_early_close_s"):
            result._number(name, positive=True)
        for name in ("prior_events", "lambda_floor", "quiet_s", "cooldown_s"):
            result._number(name)
        for name in ("burst_knocks", "first_pattern", "min_matched_rounds", "max_rounds", "max_empty_windows"):
            result._number(name, positive=True, integer=True)
        result._number("p_confirm", positive=True)
        if result.p_confirm > 1:
            raise ValueError("dialog.p_confirm must be at most one")
        for name in ("auto_challenge", "auto_interview"):
            if not isinstance(getattr(result, name), bool):
                raise ValueError(f"dialog.{name} must be boolean")
        if (not isinstance(result.patterns, list) or len(set(result.patterns)) < 2
                or any(isinstance(k, bool) or not isinstance(k, int) or k < 1 for k in result.patterns)):
            raise ValueError("dialog.patterns must contain at least two distinct positive integer counts")
        if (not isinstance(result.questions, list) or not result.questions
                or any(key not in ("q_people", "q_move", "q_hurt") for key in result.questions)
                or len(set(result.questions)) != len(result.questions)):
            raise ValueError("dialog.questions must contain distinct supported question keys")
        if not isinstance(result.profile, str) or not result.profile:
            raise ValueError("dialog.profile must be a nonempty string")
        return result

    def _number(self, name, *, positive=False, integer=False):
        value = getattr(self, name)
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                or value < 0 or (positive and value == 0) or (integer and not isinstance(value, int))):
            raise ValueError(f"dialog.{name} must be a {'positive' if positive else 'nonnegative'} "
                             f"{'integer' if integer else 'finite number'}")


@dataclass
class _Window:
    kind: str
    opened: float
    deadline: float
    times: list[float]


class DialogEngine:
    """Deterministic evidence state machine, with no I/O or clock access.

    Supply a complete profile or dialog mapping. Mapping-only callers may pass
    ``gate_tail_s`` explicitly; zero means no external prompt gate was supplied.
    ``tick(t)`` takes the audio processing watermark, after all detections at or
    before it. This matters because raw classifier clips delay onset delivery.
    Older ticks after a current-time prompt completion are harmless no-ops.

    Model probabilities use the full configured round duration, even when the
    response window closes early. They are background-model likelihoods, not a
    posterior probability a person is present; see :mod:`heartbeat.stats`.
    """

    def __init__(self, cfg, emit, rng_seed, *, gate_tail_s=0.0):
        self.cfg = DialogConfig.parse(cfg)
        prompter = getattr(cfg, "prompter", None)
        if prompter is not None:
            gate_tail_s = prompter["gate_tail_s"]
        if not math.isfinite(float(gate_tail_s)) or gate_tail_s < 0:
            raise ValueError("gate_tail_s must be finite and nonnegative")
        self.gate_tail_s = float(gate_tail_s)
        self._emit = emit
        self._rng_seed = rng_seed
        self._rng = random.Random(rng_seed)
        self._clock = 0.0
        self._epoch_t = 0.0
        self._gate_until = 0.0
        self._initialize()
        self._emit_state(0.0)

    def _initialize(self):
        cfg = self.cfg
        self.background = BackgroundRate(cfg.rate_window_s, cfg.prior_events, cfg.prior_s, cfg.lambda_floor)
        self.state, self.status = "LISTENING", "NO_SIGNS"
        self._state_since = self._clock
        self.round = 0
        self.rounds: list[dict] = []
        self.combined_p = 1.0
        self.answers: dict = {}
        self.approved = False
        self._expected = None
        self._window: _Window | None = None
        self._empty_windows = 0
        self._burst: list[float] = []
        self._burst_ready = False
        self._last_passive_knock = None
        self._cooldown_until = None
        self._awaiting_prompts = False
        self._prompt_kind = None
        self._resume_state = None
        self._questions: list[str] = []
        self._question = None

    def snapshot(self) -> dict:
        return {
            "state": self.state, "status": self.status,
            "round": self.round, "rounds": deepcopy(self.rounds),
            "combined_p": self.combined_p, "odds": format_odds(self.combined_p),
            "answers": dict(self.answers), "approved": self.approved,
            "lambda_per_min": self.background.lambda_per_s * 60,
            "profile": self.cfg.profile,
        }

    def gate_until(self, t_end: float) -> None:
        """Set an external gate end; infinity is closed by a later finite call."""
        t_end = float(t_end)
        if math.isnan(t_end) or t_end < 0:
            raise ValueError("gate end must be nonnegative or infinity")
        self._gate_until = t_end

    def _advance(self, t):
        if t < self._clock:
            return
        if self.state == "LISTENING":
            start = max(self._clock, self._gate_until)
            if start <= t:
                self.background.observe(start, t)
        self.background.advance(t)
        self._clock = t

    def _emit_state(self, t):
        self._emit("state", float(t), self.snapshot())

    def _transition(self, state, t, *, status=None):
        changed = state != self.state or (status is not None and status != self.status)
        if state != self.state:
            self._state_since = float(t)
        self.state = state
        if status is not None:
            self.status = status
        if changed:
            self._emit_state(t)

    def _error(self, t, message, command=None):
        data = {"message": message}
        if command is not None:
            data["command"] = str(command)
        self._emit("error", float(t), data)
        return []

    def _close_at(self):
        window = self._window
        early = self.cfg.early_close_s if window.kind == "round" else self.cfg.question_early_close_s
        return min(window.deadline, max(window.times) + early) if window.times else window.deadline

    def on_sound(self, det) -> list[Action]:
        t = float(det.t)
        if not math.isfinite(t) or t < self._epoch_t:
            return []
        actions = []
        if self._window is not None and t > self._close_at():
            close = self._close_at()
            self._advance(close)
            actions = self._finish_window(close)
        self._advance(t)
        if not det.is_knock or det.gated:
            return actions
        self.background.add(t)
        if self._window is not None and self._window.opened <= t <= self._window.deadline:
            self._window.times.append(t)
            self._window.times.sort()
        elif self.state == "LISTENING" and t >= self._state_since and t >= self._gate_until:
            self._burst = [previous for previous in self._burst if previous >= t - self.cfg.burst_window_s]
            self._burst.append(t)
            self._last_passive_knock = t
            if len(self._burst) >= self.cfg.burst_knocks:
                self._burst_ready = True
                self._transition("LISTENING", t, status="POSSIBLE")
        return actions

    def tick(self, t: float) -> list[Action]:
        t = float(t)
        if not math.isfinite(t) or t < self._clock:
            return []
        if self._window is not None and t >= self._close_at():
            close = self._close_at()
            self._advance(close)
            actions = self._finish_window(close)
            self._advance(t)
            return actions
        if self.state == "NO_RESPONSE" and t >= self._cooldown_until:
            end = self._cooldown_until
            self._advance(end)
            self._transition("LISTENING", end)
        if (self.state == "LISTENING" and self.cfg.auto_challenge and self._burst_ready
                and self._last_passive_knock is not None and t >= self._last_passive_knock + self.cfg.quiet_s):
            start = max(self._clock, self._last_passive_knock + self.cfg.quiet_s)
            self._advance(start)
            actions = self._start_challenge(start)
            self._advance(t)
            return actions
        self._advance(t)
        return []

    def on_prompts_finished(self, t: float) -> list[Action]:
        t = float(t)
        if not math.isfinite(t) or t < self._epoch_t:
            return []
        if not self._awaiting_prompts:
            return []
        self._advance(t)
        self._gate_until = max(self._gate_until, t + self.gate_tail_s)
        self._awaiting_prompts = False
        kind = self._prompt_kind
        self._prompt_kind = None
        if kind in ("round", "question"):
            duration = self.cfg.window_s if kind == "round" else self.cfg.question_window_s
            self._window = _Window(kind, t, t + duration, [])
            if kind == "round":
                self._transition("RESPONSE_WINDOW", t)
            data = {"kind": kind, "start": t, "end": t + duration}
            if kind == "round":
                data.update({"round": self.round, "expected": self._expected})
            else:
                data["question"] = self._question
            self._emit("window_open", t, data)
        elif kind == "reassure":
            self._transition(self._resume_state, t)
            if self.state == "NO_RESPONSE":
                self._cooldown_until = t + self.cfg.cooldown_s
            self._resume_state = None
        return []

    def _start_challenge(self, t):
        self.round, self.rounds, self.combined_p = 0, [], 1.0
        self._expected = None
        self._empty_windows = 0
        self.approved = False
        self._burst, self._burst_ready = [], False
        return self._next_round(t)

    def _next_round(self, t):
        first = self.round == 0
        choices = [k for k in self.cfg.patterns if k != self._expected]
        self._expected = self.cfg.first_pattern if first else self._rng.choice(choices)
        self.round += 1
        self._window = None
        self._awaiting_prompts, self._prompt_kind = True, "round"
        self._transition("PROMPTING", t)
        knocks = PromptKnocks(self._expected, self.cfg.interval_s)
        return [PromptVoice("copy_knocks"), knocks] if first else [knocks]

    def _finish_window(self, t):
        window = self._window
        self._window = None
        if window.kind == "question":
            heard = len(window.times)
            if self._question == "q_people":
                value = heard if heard else "no answer"
            else:
                value = {1: "no", 2: "yes"}.get(heard, "unclear")
            self.answers[self._question] = value
            self._emit("answer", t, {"question": self._question, "heard": heard, "value": value})
            if self._questions:
                return self._next_question(t)
            self._question = None
            self._transition("CONFIRMED", t)
            return []

        heard = len(window.times)
        p = round_p(self._expected, heard, self.cfg.window_s, self.background.lambda_per_s)
        matched = p is not None
        previous = [record["p"] for record in self.rounds if record["matched"]]
        self.combined_p = combine(previous + ([p] if matched else []))
        record = {"round": self.round, "expected": self._expected, "heard": heard,
                  "knock_times": list(window.times), "p": p, "matched": matched,
                  "combined_p": self.combined_p, "odds": format_odds(self.combined_p)}
        self.rounds.append(record)
        self._emit("round_result", t, deepcopy(record))
        self._empty_windows = self._empty_windows + 1 if heard == 0 else 0
        matches = [result for result in self.rounds if result["matched"]]
        if (len(matches) >= self.cfg.min_matched_rounds
                and len({result["expected"] for result in matches}) >= 2
                and self.combined_p <= self.cfg.p_confirm):
            self._transition("CONFIRMED", t, status="CONFIRMED")
            if self.cfg.auto_interview:
                return self._start_interview(t, self.cfg.questions, intro=True)
            self._awaiting_prompts, self._prompt_kind = True, "confirmation"
            return [PromptVoice("we_hear_you")]
        if len(self.rounds) >= self.cfg.max_rounds or self._empty_windows >= self.cfg.max_empty_windows:
            self._cooldown_until = t + self.cfg.cooldown_s
            self._transition("NO_RESPONSE", t, status="POSSIBLE" if matches else "NO_SIGNS")
            return []
        if matched:
            self.status = "POSSIBLE"
        return self._next_round(t)

    def _start_interview(self, t, questions, *, intro=False):
        self._questions = list(questions)
        self._transition("INTERVIEW", t)
        actions = self._next_question(t)
        return ([PromptVoice("we_hear_you")] if intro else []) + actions

    def _next_question(self, t):
        self._question = self._questions.pop(0)
        self._awaiting_prompts, self._prompt_kind = True, "question"
        self._emit_state(t)
        return [PromptVoice(self._question)]

    def command(self, name, t, **args) -> list[Action]:
        try:
            t = float(t)
        except (TypeError, ValueError):
            return self._error(self._clock, "operator command requires a finite stream time", name)
        if not math.isfinite(t) or t < self._clock:
            return self._error(self._clock, "operator command time must not precede current stream time", name)
        self._advance(t)
        allowed = {"challenge": set(), "interview": set(), "ask": {"q"},
                   "reassure": set(), "reset": set(), "approve": {"operator"}}
        if not isinstance(name, str) or name not in allowed:
            return self._error(t, "unknown operator command", name)
        if set(args) - allowed[name]:
            return self._error(t, "unknown operator command arguments", name)
        idle = not self._awaiting_prompts and self._window is None
        if name == "reset":
            self._epoch_t = t
            self._rng = random.Random(self._rng_seed)
            self._initialize()
            self._emit("operator", t, {"command": name})
            self._emit_state(t)
            return []
        if name == "approve":
            if self.state != "CONFIRMED":
                return self._error(t, "approve requires CONFIRMED state", name)
            operator = args.get("operator")
            if operator is not None and not isinstance(operator, str):
                return self._error(t, "operator initials must be a string", name)
            self.approved = True
            self._emit("approval", t, {"approved": True, "operator": operator})
            self._emit_state(t)
            return []
        if name == "challenge":
            if self.state not in ("LISTENING", "NO_RESPONSE") or not idle:
                return self._error(t, "challenge requires idle LISTENING or NO_RESPONSE state", name)
            self._emit("operator", t, {"command": name})
            return self._start_challenge(t)
        if name in ("interview", "ask"):
            if self.state != "CONFIRMED" or not idle:
                return self._error(t, f"{name} requires idle CONFIRMED state", name)
            questions = self.cfg.questions if name == "interview" else [args.get("q")]
            if any(key not in ("q_people", "q_move", "q_hurt") for key in questions):
                return self._error(t, "ask requires a supported question key", name)
            self._emit("operator", t, {"command": name, **args})
            return self._start_interview(t, questions)
        if self.state not in ("LISTENING", "NO_RESPONSE", "CONFIRMED") or not idle:
            return self._error(t, "reassure requires an idle state", name)
        self._emit("operator", t, {"command": name})
        self._resume_state = self.state
        self._awaiting_prompts, self._prompt_kind = True, "reassure"
        self._transition("PROMPTING", t)
        return [PromptVoice("reassure")]
