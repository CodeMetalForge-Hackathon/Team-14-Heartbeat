from dataclasses import replace
import hashlib
import json
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import numpy as np
import pytest
import soundfile as sf

from heartbeat.audio import AudioBlock
from heartbeat.blackbox import BlackBox, canonical_json, compare_sounds, replay_run, report_run, verify_run
from heartbeat.config import load_profile
from heartbeat.events import EventBus
from heartbeat.runner import run_simulation
from heartbeat.synth import knock


def profile_with(**overrides):
    profile = load_profile()
    return replace(profile, server={**profile.server, "blackbox": {**profile.server["blackbox"], **overrides}})


def clip():
    x = np.zeros(15600, dtype=np.float32)
    sound = knock(seed=17)
    x[1600 : 1600 + len(sound)] = sound
    return x


def small_run(path, *, levels=False):
    profile = load_profile()
    bus = EventBus()
    box = BlackBox(path, profile, ["hb", "sim"], bus=bus)
    unsubscribe = bus.subscribe(box)
    bus.publish("session_start", 0)
    if levels:
        for index in range(30):
            bus.publish("level", index / 10, dbfs=-50, floor_db=-52)
    event = bus.publish("sound", 1, snr_db=25, peak=0.8, gated=False, score=1.0,
                        is_knock=True, classifier="none", top=[], clip=None, _clip=clip())
    for second in range(3):
        box.on_audio(AudioBlock(float(second), np.zeros(16000, dtype=np.float32)))
    bus.publish("state", 3, state="CONFIRMED", status="CONFIRMED", lambda_per_min=1,
                odds="1 in 2.3 million", answers={"q_people": 2, "q_move": "yes", "q_hurt": "no"})
    bus.publish("session_end", 3)
    unsubscribe()
    box.close()
    return box, event


def records(path):
    return [json.loads(line) for line in (path / "events.jsonl").read_text().splitlines()]


def test_chain_and_clip_hashes_match_exact_contract_and_do_not_mutate_event(tmp_path):
    path = tmp_path / "run"
    box, event = small_run(path)
    check = verify_run(path)
    assert check.ok, str(check)
    assert str(check) == "OK 4 events, 1 clips"
    previous = "0" * 64
    for record in records(path):
        payload = {key: record[key] for key in ("seq", "t", "wall", "type", "data")}
        expected = hashlib.sha256((previous + canonical_json(payload)).encode()).hexdigest()
        assert record["prev"] == previous and record["hash"] == expected
        previous = expected
    saved = next(record for record in records(path) if record["type"] == "sound")
    assert saved["data"]["clip"] == f"clips/{saved['seq']}.wav"
    data = (path / saved["data"]["clip"]).read_bytes()
    assert saved["data"]["clip_sha256"] == hashlib.sha256(data).hexdigest()
    info = sf.info(path / saved["data"]["clip"])
    assert info.frames == 15600 and info.samplerate == 16000 and info.subtype == "PCM_16"
    assert event.data["clip"] is None and event.clip.shape == (15600,)
    assert box.status()["closed"] and box.status()["integrity_valid"]


def test_metadata_contains_reproducibility_and_final_custody_information(tmp_path):
    path = tmp_path / "run"
    box, _ = small_run(path)
    meta = json.loads((path / "meta.json").read_text())
    assert {"profile", "argv", "git_commit", "hostname", "platform", "python", "packages", "start_time"} <= meta.keys()
    assert meta["argv"] == ["hb", "sim"]
    assert meta["profile"]["audio"]["sample_rate"] == 16000
    assert meta["completed"] and meta["closed"] and meta["integrity_valid"]
    assert meta["events"] == box.status()["events"]
    assert meta["final_hash"] == records(path)[-1]["hash"]
    json.dumps(box.status(), allow_nan=False)


def test_editing_json_payload_is_detected_at_exact_sequence(tmp_path):
    path = tmp_path / "run"
    small_run(path)
    saved = records(path)
    target = next(record for record in saved if record["type"] == "sound")
    target["data"]["peak"] = 0.123
    (path / "events.jsonl").write_text("".join(json.dumps(record) + "\n" for record in saved))
    check = verify_run(path)
    assert not check.ok and check.seq == target["seq"]
    assert "record hash mismatch" in str(check)


def test_editing_one_clip_byte_is_detected_at_its_sequence(tmp_path):
    path = tmp_path / "run"
    small_run(path)
    target = next(record for record in records(path) if record["type"] == "sound")
    clip_path = path / target["data"]["clip"]
    changed = bytearray(clip_path.read_bytes())
    changed[-1] ^= 1
    clip_path.write_bytes(changed)
    check = verify_run(path)
    assert not check.ok and check.seq == target["seq"]
    assert check.reason == "clip hash mismatch"


def test_truncation_and_raw_edits_are_detected(tmp_path):
    path = tmp_path / "truncate"
    small_run(path)
    lines = (path / "events.jsonl").read_text().splitlines()
    (path / "events.jsonl").write_text("\n".join(lines[:-1]) + "\n")
    assert "possible truncation" in verify_run(path).reason
    raw_path = tmp_path / "raw-edit"
    small_run(raw_path)
    changed = bytearray((raw_path / "raw.wav").read_bytes())
    changed[-1] ^= 1
    (raw_path / "raw.wav").write_bytes(changed)
    assert verify_run(raw_path).reason == "raw audio hash mismatch"


def test_levels_are_saved_at_most_once_per_second(tmp_path):
    path = tmp_path / "run"
    small_run(path, levels=True)
    levels = [record["t"] for record in records(path) if record["type"] == "level"]
    assert levels == [0, 1, 2]
    assert verify_run(path).ok


def test_raw_header_is_valid_after_one_stream_second_before_close(tmp_path):
    path = tmp_path / "run"
    box = BlackBox(path, load_profile(), [])
    box.on_audio(AudioBlock(0, np.zeros(16000, dtype=np.float32)))
    deadline = time.monotonic() + 5
    while box.status()["raw_flushed_samples"] < 16000 and time.monotonic() < deadline:
        time.sleep(0.001)
    try:
        assert not box.status()["closed"]
        assert box.status()["raw_flushed_samples"] == 16000
        assert sf.info(path / "raw.wav").frames == 16000
        box.on_audio(AudioBlock(1, np.zeros(8000, dtype=np.float32)))
    finally:
        box.close()
    assert sf.info(path / "raw.wav").duration == 1.5
    assert box.status()["raw_flushed_samples"] == 24000
    assert verify_run(path).ok


def test_publish_and_raw_hook_do_not_wait_for_stalled_writer(tmp_path, monkeypatch):
    entered, release, published = threading.Event(), threading.Event(), threading.Event()
    original = BlackBox._write_event

    def stalled(self, *args):
        entered.set()
        assert release.wait(5)
        return original(self, *args)

    monkeypatch.setattr(BlackBox, "_write_event", stalled)
    bus = EventBus()
    box = BlackBox(tmp_path / "run", load_profile(), [], bus=bus)
    bus.subscribe(box)
    bus.publish("session_start", 0)
    assert entered.wait(5)

    def publish_more():
        bus.publish("operator", 0.1, command="challenge")
        box.on_audio(AudioBlock(0, np.zeros(256, dtype=np.float32)))
        published.set()

    thread = threading.Thread(target=publish_more)
    thread.start()
    try:
        assert published.wait(1), "publisher waited for file I/O"
    finally:
        release.set()
        thread.join()
        box.close()
    assert verify_run(tmp_path / "run").ok


def test_overflow_is_nonblocking_emits_error_and_invalidates_metadata(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = BlackBox._write_event

    def stalled(self, *args):
        entered.set()
        assert release.wait(5)
        return original(self, *args)

    monkeypatch.setattr(BlackBox, "_write_event", stalled)
    bus = EventBus()
    errors = []
    bus.subscribe(lambda event: errors.append(event) if event.type == "error" else None)
    box = BlackBox(tmp_path / "run", profile_with(queue_size=1), [], bus=bus)
    bus.subscribe(box)
    bus.publish("session_start", 0)
    assert entered.wait(5)
    bus.publish("operator", 0.1, command="challenge")
    bus.publish("operator", 0.2, command="challenge")
    assert errors and "overflow" in errors[-1].data["message"]
    assert not box.status()["integrity_valid"] and box.status()["dropped_events"] == 1
    release.set()
    box.close()
    check = verify_run(tmp_path / "run")
    assert not check.ok and "overflow" in check.reason
    assert any(record["type"] == "error" for record in records(tmp_path / "run"))


def test_queued_audio_clips_and_event_data_are_copied_before_source_reuse(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = BlackBox._write_event

    def stalled(self, *args):
        entered.set()
        assert release.wait(5)
        return original(self, *args)

    monkeypatch.setattr(BlackBox, "_write_event", stalled)
    path = tmp_path / "run"
    bus = EventBus()
    box = BlackBox(path, load_profile(), [], bus=bus)
    bus.subscribe(box)
    bus.publish("session_start", 0)
    assert entered.wait(5)
    original_clip = clip()
    event = bus.publish("sound", 0, peak=0.8, is_knock=True, _clip=original_clip)
    raw = np.full(256, 0.2, dtype=np.float32)
    box.on_audio(AudioBlock(0, raw))
    event.data["peak"] = 0
    original_clip[:] = 0
    raw[:] = 0
    release.set()
    box.close()
    saved = next(record for record in records(path) if record["type"] == "sound")
    assert saved["data"]["peak"] == 0.8
    assert np.max(np.abs(sf.read(path / saved["data"]["clip"])[0])) > 0.5
    np.testing.assert_allclose(sf.read(path / "raw.wav")[0], 0.2, atol=1 / 32768)
    assert verify_run(path).ok


def test_writer_failure_is_reported_without_crashing_publish_or_shutdown(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(sf, "write", broken)
    bus = EventBus()
    errors = []
    bus.subscribe(lambda event: errors.append(event) if event.type == "error" else None)
    box = BlackBox(tmp_path / "run", load_profile(), [], bus=bus)
    bus.subscribe(box)
    bus.publish("sound", 0, _clip=clip(), is_knock=True)
    box.close()
    box.close()
    assert box.status()["closed"] and box.status()["failed"]
    assert not box.status()["integrity_valid"]
    assert any("simulated disk failure" in event.data["message"] for event in errors)
    assert not verify_run(tmp_path / "run").ok


def test_all_file_writes_run_on_one_writer_thread(tmp_path, monkeypatch):
    threads = []
    for name in ("_write_meta", "_write_event", "_write_audio"):
        original = getattr(BlackBox, name)

        def track(self, *args, _original=original):
            threads.append(threading.get_ident())
            return _original(self, *args)

        monkeypatch.setattr(BlackBox, name, track)
    small_run(tmp_path / "run")
    assert len(set(threads)) == 1 and threading.get_ident() not in threads


def test_existing_run_is_never_overwritten(tmp_path):
    path = tmp_path / "run"
    small_run(path)
    original = (path / "events.jsonl").read_bytes()
    box = BlackBox(path, load_profile(), [])
    box.close()
    assert box.status()["failed"]
    assert (path / "events.jsonl").read_bytes() == original
    assert verify_run(path).ok


def test_replay_comparison_is_one_to_one_inclusive_30ms_and_shows_changed_classification():
    old = [{"t": 1.0, "is_knock": True}, {"t": 2.0, "is_knock": True}]
    new = [{"t": 1.03, "is_knock": False}, {"t": 3.0, "is_knock": True}]
    result = compare_sounds(old, new, 0.03)
    assert (result["matched"], result["missing"], result["new"], result["changed"]) == (1, 1, 1, 1)
    assert result["missing_times"] == [2.0] and result["new_times"] == [3.0]


def test_simulation_recording_verifies_duration_replays_and_reports(tmp_path):
    path = tmp_path / "sim"
    profile = load_profile()
    result = run_simulation(profile, people=2, can_move="yes", hurt="no", seed=1,
                            run_dir=path, argv=["hb", "sim", "--people", "2"])
    check = verify_run(path)
    assert check.ok, str(check)
    assert check.clips > 0 and check.events > 0
    assert abs(sf.info(path / "raw.wav").duration - result.t) < 1
    replay = replay_run(path)
    assert replay["matched"] == check.clips
    assert replay["missing"] == replay["new"] == 0
    report = report_run(path).read_text()
    for heading in ("Duration", "Sounds and knocks", "Background rate", "Rounds", "Final odds, answers and approvals", "Errors"):
        assert f"## {heading}" in report
    assert '"q_people": 2' in report and '"q_hurt": "no"' in report
    assert "tamper-evident" in report and "external copy" in report


def test_standalone_wav_replay_and_invalid_recording_rejection(tmp_path):
    profile = load_profile()
    profile = replace(profile, classifier=replace(profile.classifier, name="none"))
    x = np.zeros(3 * 16000, dtype=np.float32)
    sound = knock(seed=2)
    x[16000 : 16000 + len(sound)] = sound
    wav = tmp_path / "standalone.wav"
    sf.write(wav, x, 16000)
    replay = replay_run(wav, profile=profile)
    assert replay["original"] == replay["matched"] == replay["missing"] == 0
    assert replay["new"] == replay["replayed"] == 1
    path = tmp_path / "invalid"
    small_run(path)
    (path / "events.jsonl").write_text("not-json\n")
    check = verify_run(path)
    assert not check.ok and check.seq == 1
    with pytest.raises(ValueError, match="TAMPERED"):
        replay_run(path)


def test_non_object_metadata_fails_verification_without_raising(tmp_path):
    path = tmp_path / "run"
    small_run(path)
    (path / "meta.json").write_text("[]\n")
    check = verify_run(path)
    assert not check.ok and check.seq == 0 and "metadata must be an object" in check.reason


def test_ctrl_c_during_simulated_prompt_drains_and_verifies_the_recording(tmp_path):
    path = tmp_path / "interrupted"
    script = (
        "import sys; from heartbeat.runner import run_simulation; "
        "run_simulation(realtime=True, run_dir=sys.argv[1], duration_s=600); print('DONE')"
    )
    process = subprocess.Popen([sys.executable, "-c", script, str(path)],
                               cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 15
    prompting = False
    try:
        while process.poll() is None and time.monotonic() < deadline:
            log = path / "events.jsonl"
            if log.exists() and '"type":"prompt_start"' in log.read_text():
                prompting = True
                break
            time.sleep(0.01)
        assert prompting, "synthetic session did not start its challenge"
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert "DONE" in stdout
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    check = verify_run(path)
    assert check.ok, str(check)
    saved = records(path)
    assert any(record["type"] == "prompt_end" and record["data"].get("interrupted") for record in saved)
    assert saved[-1]["type"] in ("session_end", "state")


def test_live_runtime_records_sounds_without_retaining_event_clips(tmp_path):
    from heartbeat.audio import WavSource
    from heartbeat.prompter import FakePrompter
    from heartbeat.runner import prepare_runtime

    profile = load_profile()
    profile = replace(profile, classifier=replace(profile.classifier, name="none"))
    audio = np.zeros(3 * 16000, dtype=np.float32)
    sound = knock(seed=2)
    audio[16000:16000 + len(sound)] = sound
    wav = tmp_path / "input.wav"
    sf.write(wav, audio, 16000)
    path = tmp_path / "live"
    runtime = prepare_runtime(
        profile, lambda: WavSource(wav),
        lambda pipeline: FakePrompter(
            pipeline.bus, cfg=profile.prompter, clock=lambda: pipeline.t, no_survivor=True,
        ),
        run_dir=path, argv=["hb", "run", "--wav", str(wav)],
    )
    try:
        result = runtime.run()
    finally:
        runtime.close()
    assert runtime.events == [] and result.events == []
    saved = [record for record in records(path) if record["type"] == "sound"]
    assert saved
    assert all(sf.info(path / record["data"]["clip"]).frames == 15600 for record in saved)
    check = verify_run(path)
    assert check.ok and check.clips == len(saved), str(check)
