"""P05 checks use synthesized audio and fake inference; never fetch models."""

import csv
from dataclasses import replace
import json
from pathlib import Path

import numpy as np
import pytest

from heartbeat import synth
from heartbeat.classify import make_classifier
from heartbeat.classifiers.features import extract_features, feature_names
from heartbeat.classifiers.heuristic import HeuristicClassifier
from heartbeat.classifiers.yamnet import YamnetClassifier
from heartbeat.config import load_profile
from heartbeat.onset import CLIP_SAMPLES, PRE_SAMPLES, Onset


def onset(sound, *, gated=False):
    clip = np.zeros(CLIP_SAMPLES, dtype=np.float32)
    size = min(len(sound), CLIP_SAMPLES - PRE_SAMPLES)
    clip[PRE_SAMPLES:PRE_SAMPLES + size] = sound[:size]
    return Onset(t=1.25, peak=float(np.max(np.abs(clip))), snr_db=25.0,
                 gated=gated, clip=clip)


@pytest.fixture
def cfg():
    return load_profile().classifier


@pytest.fixture
def fake_model(tmp_path, cfg):
    labels = [f"Class {i}" for i in range(521)]
    for index, label in ((17, "Knock"), (23, "Tap"), (49, "Thump, thud")):
        labels[index] = label
    with (tmp_path / "yamnet_class_map.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["index", "mid", "display_name"])
        writer.writerows((i, f"/fake/{i}", label) for i, label in enumerate(labels))
    return replace(cfg, model_dir=str(tmp_path), head_path=str(tmp_path / "head.joblib"))


class FakeInterpreter:
    def __init__(self, *, embedding=True):
        self.embedding = embedding
        self.input = None
        self.invocations = 0

    def allocate_tensors(self):
        pass

    def get_input_details(self):
        return [{"index": 3, "name": "waveform", "shape": np.array([CLIP_SAMPLES]), "dtype": np.float32}]

    def get_output_details(self):
        # Deliberately reordered indices; embeddings cannot be assumed output 1.
        outputs = [{"index": 12, "name": "scores", "shape": np.array([2, 521]), "dtype": np.float32}]
        if self.embedding:
            outputs.insert(0, {"index": 8, "name": "embeddings", "shape": np.array([2, 1024]), "dtype": np.float32})
        return outputs

    def set_tensor(self, index, value):
        assert index == 3
        assert value.dtype == np.float32
        self.input = value.copy()

    def invoke(self):
        self.invocations += 1

    def get_tensor(self, index):
        if index == 8:
            return np.ones((2, 1024), dtype=np.float32)
        assert index == 12
        scores = np.zeros((2, 521), dtype=np.float32)
        scores[:, 17] = (0.6, 0.8)
        scores[:, 23] = 0.2
        scores[:, 49] = 0.3
        scores[:, 500] = 0.9
        return scores


def test_features_are_finite_and_have_required_layout(cfg):
    names = feature_names(cfg)
    assert len(names) == 39
    assert len([name for name in names if name.startswith("log_mel_")]) == 32
    values = extract_features(onset(synth.knock(seed=4)).clip, cfg)
    assert values.shape == (39,)
    assert np.isfinite(values).all()
    assert values[0] >= 0 and values[1] >= 0
    assert 0 <= values[2] <= 8000
    assert values[3:6].sum() == pytest.approx(1)
    assert np.isfinite(extract_features(np.zeros(CLIP_SAMPLES), cfg)).all()


@pytest.mark.parametrize("bad", [np.array([np.nan]), np.array([np.inf]), np.zeros((2, 3))])
def test_features_reject_invalid_audio(cfg, bad):
    with pytest.raises(ValueError):
        extract_features(bad, cfg)


def test_heuristic_synthetic_auc_over_point_nine(cfg):
    classifier = HeuristicClassifier(cfg)
    positive = [classifier.classify(onset(synth.knock(seed=i))).score for i in range(32)]
    negative = [classifier.classify(onset(generator(seed=i))).score
                for generator in (synth.clap, synth.speech_like) for i in range(32)]
    difference = np.asarray(positive)[:, None] - np.asarray(negative)
    auc = float(np.mean((difference > 0) + 0.5 * (difference == 0)))
    assert auc > 0.9


def test_heuristic_preserves_onset_and_json_types(cfg):
    source = onset(synth.knock(seed=2), gated=True)
    result = HeuristicClassifier(cfg).classify(source)
    assert result.clip is source.clip
    assert result.t == source.t and result.gated is True
    assert result.classifier == "heuristic"
    assert result.is_knock == (result.score >= cfg.threshold)
    assert 0 <= result.score <= 1
    assert len(result.top) <= 5
    json.dumps({"score": result.score, "is_knock": result.is_knock, "top": result.top}, allow_nan=False)


@pytest.mark.parametrize("embedding", [False, True])
def test_yamnet_uses_display_names_and_actual_output_shapes(fake_model, embedding):
    fake = FakeInterpreter(embedding=embedding)
    classifier = YamnetClassifier(fake_model, interpreter=fake)
    source = onset(synth.knock(seed=3), gated=True)
    result = classifier.classify(source)
    assert result.score == pytest.approx(0.7)
    assert result.top[0][0] == "Class 500"
    assert len(result.top) == 5
    assert result.gated and result.clip is source.clip
    assert fake.invocations == 1
    assert classifier.feature_spec["embedding"] == (1024 if embedding else 0)
    np.testing.assert_array_equal(fake.input, source.clip)


def test_yamnet_unknown_targets_are_rejected(fake_model):
    with pytest.raises(ValueError, match="target"):
        YamnetClassifier(replace(fake_model, targets=["Not an AudioSet class"]), interpreter=FakeInterpreter())


def test_yamnet_rejects_incompatible_input(fake_model):
    fake = FakeInterpreter()
    fake.get_input_details = lambda: [{"index": 0, "name": "wrong", "shape": [100], "dtype": np.float32}]
    with pytest.raises(ValueError, match="15600"):
        YamnetClassifier(fake_model, interpreter=fake)


def test_yamnet_rejects_nonfinite_predictions(fake_model):
    fake = FakeInterpreter(embedding=False)
    fake.get_tensor = lambda index: np.full((1, 521), np.nan)
    classifier = YamnetClassifier(fake_model, interpreter=fake)
    with pytest.raises(ValueError, match="non-finite"):
        classifier.extract(np.zeros(CLIP_SAMPLES))


def test_auto_without_models_uses_heuristic(tmp_path, cfg):
    classifier = make_classifier(replace(cfg, model_dir=str(tmp_path), head_path=str(tmp_path / "missing.joblib")))
    assert classifier.name == "heuristic"
    assert classifier.classify(onset(synth.knock(seed=3))).classifier == "heuristic"


def test_auto_bad_head_and_model_still_uses_heuristic(tmp_path, cfg):
    (tmp_path / "head.joblib").write_bytes(b"invalid artifact")
    (tmp_path / "yamnet.tflite").write_bytes(b"invalid model")
    classifier = make_classifier(replace(cfg, model_dir=str(tmp_path), head_path=str(tmp_path / "head.joblib")))
    assert classifier.name == "heuristic"


def test_auto_retires_failed_runtime_and_retries_on_heuristic(fake_model, monkeypatch):
    import heartbeat.classifiers.yamnet as module

    (Path(fake_model.model_dir) / "yamnet.tflite").write_bytes(b"fake model for file selection only")

    class BrokenYamnet:
        name = "yamnet"

        def __init__(self, cfg):
            pass

        def classify(self, source):
            raise RuntimeError("simulated interpreter failure")

    monkeypatch.setattr(module, "YamnetClassifier", BrokenYamnet)
    classifier = make_classifier(fake_model)
    assert classifier.name == "yamnet"
    assert classifier.classify(onset(synth.knock(seed=9))).classifier == "heuristic"
    assert classifier.name == "heuristic"


def test_real_yamnet_when_local_model_is_available(cfg):
    if not (Path(cfg.model_dir) / "yamnet.tflite").is_file():
        pytest.skip("YAMNet is absent; tests never download it")
    try:
        classifier = YamnetClassifier(cfg)
    except RuntimeError as exc:
        if "No CPU TFLite interpreter" in str(exc):
            pytest.skip(str(exc))
        raise
    result = classifier.classify(onset(synth.knock(seed=8)))
    assert 0 <= result.score <= 1
    assert len(result.top) == 5


def test_grouped_splits_and_calibration_never_share_recordings(cfg):
    pytest.importorskip("sklearn")
    from tools.train_head import calibration_split, grouped_splits

    labels = np.repeat([0, 1], 24)
    groups = np.repeat([f"recording{i}" for i in range(16)], 3)
    development, calibration = calibration_split(labels, groups, 0.25, 42)
    calibration_groups = set(groups[calibration])
    assert not calibration_groups.intersection(groups[development])
    tested = []
    for train, test in grouped_splits(labels[development], groups[development], 5, 42):
        fit_groups = set(groups[development[train]])
        test_groups = set(groups[development[test]])
        assert not fit_groups.intersection(test_groups)
        assert not calibration_groups.intersection(fit_groups | test_groups)
        assert set(labels[development[train]]) == {0, 1}
        tested.extend(development[test])
    assert sorted(tested) == sorted(development)


def test_threshold_ties_respect_inclusive_score_contract():
    from tools.train_head import ThresholdUnattainableError, choose_threshold

    labels = np.array([0, 0, 0, 0, 1, 1])
    scores = np.array([0.1, 0.2, 0.8, 0.8, 0.85, 0.95])
    threshold = choose_threshold(labels, scores, 0.25)
    assert np.mean(scores[labels == 0] >= threshold) <= 0.25
    assert threshold > 0.8
    with pytest.raises(ThresholdUnattainableError):
        choose_threshold([0, 1], [1.0, 1.0], 0.02)
    with pytest.raises(ValueError):
        choose_threshold([1, 1], [0.8, 0.9], 0.02)


def test_grouped_splits_reject_conflicting_source_labels():
    pytest.importorskip("sklearn")
    from tools.train_head import grouped_splits

    with pytest.raises(ValueError, match="conflicting labels"):
        grouped_splits([0, 1, 0, 1], ["same", "same", "b", "c"], 2, 42)


def test_head_scaling_limits_rare_scores_without_changing_dsp(cfg):
    pytest.importorskip("sklearn")
    from sklearn.preprocessing import StandardScaler
    from tools.train_head import fit_head

    rng = np.random.default_rng(42)
    # A sub-percent class-score change must not become an enormous z-score.
    scores = rng.uniform(0, 0.0002, (40, 521))
    rest = rng.normal(size=(40, 41))  # Two embedding values plus 39 DSP features.
    features = np.column_stack((scores, rest))
    model = fit_head(features, np.tile([0, 1], 20), cfg)
    scale = model.named_steps["scaler"].scale_
    np.testing.assert_allclose(scale[:521], cfg.training["score_scale_floor"])
    np.testing.assert_allclose(scale[521:], StandardScaler().fit(rest).scale_)
    small = fit_head(rest[:, :3], np.tile([0, 1], 20), cfg)
    np.testing.assert_allclose(small.named_steps["scaler"].scale_, StandardScaler().fit(rest[:, :3]).scale_)


@pytest.mark.parametrize("embedding", [False, True])
def test_head_trains_saves_loads_with_synthetic_clips(fake_model, tmp_path, embedding):
    pytest.importorskip("sklearn")
    pytest.importorskip("joblib")
    import soundfile as sf
    from heartbeat.classifiers.head import HeadClassifier, load_head
    from tools.segment import INDEX_FIELDS
    from tools.train_head import train_index

    cfg = replace(fake_model, training={**fake_model.training, "speed_repeats": 2, "cv_splits": 3})
    clips = tmp_path / "clips"
    clips.mkdir()
    index_path = clips / "index.csv"
    rows = []
    for label, generator in (("knock", synth.knock), ("other", synth.clap)):
        for source_id in range(8):
            for event in range(2):
                path = f"{label}_{source_id}_{event}.wav"
                audio = onset(generator(seed=source_id * 2 + event)).clip
                sf.write(clips / path, audio, 16000, subtype="FLOAT")
                rows.append(dict(path=path, label=label, source_file=f"{label}_{source_id}.wav",
                                 surface=f"synthetic{source_id % 2}", t=str(event + 1), snr_db="25"))
    with index_path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=INDEX_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    # Human review can remove a clip without rewriting the index.
    (clips / rows[0]["path"]).unlink()
    yamnet = YamnetClassifier(cfg, interpreter=FakeInterpreter(embedding=embedding))
    report = train_index(index_path, cfg, output=cfg.head_path, report_path=tmp_path / "classifier.md",
                         yamnet=yamnet, provenance="SYNTHETIC TEST FIXTURES ONLY")
    artifact = load_head(cfg.head_path)
    assert report["missing_clips"] == 1
    assert len(artifact["training_data_hash"]) == 64
    assert artifact["feature_spec"]["yamnet"]["embedding"] == (1024 if embedding else 0)
    assert artifact["pipeline"].n_features_in_ == 521 + 39 + (1024 if embedding else 0)
    assert artifact["pipeline"].named_steps["logistic"].class_weight == "balanced"
    assert np.all(artifact["pipeline"].named_steps["scaler"].scale_[:521] >= cfg.training["score_scale_floor"])
    assert report["final_calibration"]["false_positive_rate"] <= cfg.training["max_false_positive_rate"]
    classifier = HeadClassifier(cfg, yamnet=yamnet)
    result = classifier.classify(onset(synth.knock(seed=991), gated=True))
    assert result.gated and result.classifier == "head"
    assert result.is_knock == (result.score >= cfg.threshold)
    assert classifier.calibrated_threshold == artifact["threshold"]
    assert "SYNTHETIC TEST FIXTURES ONLY" in (tmp_path / "classifier.md").read_text()
    changed = replace(cfg, features={**cfg.features, "fft_size": 1024})
    with pytest.raises(ValueError, match="feature spec"):
        HeadClassifier(changed, yamnet=yamnet)


def test_segment_preserves_labels_sources_and_flushes_eof(tmp_path):
    import soundfile as sf
    from tools.segment import read_index, resolve_index_path, segment

    raw, clips = tmp_path / "raw", tmp_path / "clips"
    raw.mkdir()
    profile = load_profile()
    for label, kind in (("knock", "knock"), ("other", "clap"), ("bg", "footstep"), ("self", "knock")):
        audio, _ = synth.scene(2.0, [(0.6, kind, 0), (1.85, kind, 0)], seed=3)
        sf.write(raw / f"{label}__test__{kind}__01.wav", audio, 16000, subtype="FLOAT")
    result = segment(raw, clips, profile)
    rows = read_index(result["index"])
    assert result["recordings"] == 4
    assert {row["label"] for row in rows} == {"knock", "other", "bg", "self"}
    assert any(float(row["t"]) > 1.8 for row in rows)
    assert len({row["path"] for row in rows}) == len(rows)
    for row in rows:
        samples, rate = sf.read(resolve_index_path(row, result["index"]))
        assert rate == 16000 and samples.shape == (CLIP_SAMPLES,)
        assert row["source_file"].startswith(row["label"] + "__")


def test_index_rejects_clip_path_escape(tmp_path):
    from tools.segment import resolve_index_path

    with pytest.raises(ValueError):
        resolve_index_path({"path": "../outside.wav"}, tmp_path / "index.csv")


def test_fetch_requires_explicit_network_approval(tmp_path, monkeypatch):
    import tools.fetch_models as fetch

    profile = load_profile()
    monkeypatch.setattr(fetch, "load_profile", lambda name: replace(profile, classifier=replace(
        profile.classifier, model_dir=str(tmp_path / "models"))))

    def forbidden(*args, **kwargs):
        pytest.fail("fetch attempted network without --allow-network")

    monkeypatch.setattr(fetch.urllib.request, "urlopen", forbidden)
    assert fetch.main([]) == 2
    assert not (tmp_path / "models").exists()


def test_review_sorts_difficult_clips_and_escapes_metadata(tmp_path):
    import soundfile as sf
    from types import SimpleNamespace
    from tools.label_review import build_review, remove_clips
    from tools.segment import INDEX_FIELDS

    clips = tmp_path / "clips"
    clips.mkdir()
    rows = []
    for label in ("knock", "other"):
        for suffix, amplitude in (("low", 0.2), ("high", 0.8)):
            name = f"{label}_{suffix}.wav"
            sf.write(clips / name, np.full(CLIP_SAMPLES, amplitude), 16000, subtype="FLOAT")
            rows.append(dict(path=name, label=label, source_file=f"{label}_{suffix}.wav",
                             surface="<script>fixture</script>", t="1", snr_db="20"))
    index = clips / "index.csv"
    with index.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=INDEX_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    class ScorePeak:
        def classify(self, source):
            return SimpleNamespace(score=source.peak, classifier="test")

    pages = tmp_path / "pages"
    result = build_review(index, pages, load_profile(), classifier=ScorePeak())
    knocks = (pages / "review_knock.html").read_text()
    others = (pages / "review_other.html").read_text()
    assert result["clips"] == 4
    assert knocks.index("knock_low.wav") < knocks.index("knock_high.wav")
    assert others.index("other_high.wav") < others.index("other_low.wav")
    assert "&lt;script&gt;fixture&lt;/script&gt;" in knocks
    assert '<audio controls preload="none" src="../clips/knock_low.wav"' in knocks
    removal = tmp_path / "remove.json"
    removal.write_text(json.dumps(["knock_low.wav", "../outside.wav"]))
    with pytest.raises(ValueError):
        remove_clips(index, removal)
    assert (clips / "knock_low.wav").is_file()


def test_background_rates_and_missing_dialog_are_explicit(tmp_path, monkeypatch):
    import soundfile as sf
    from heartbeat.classify import PassThroughClassifier
    import tools.eval_background as module

    audio, _ = synth.scene(2.0, [(0.6, "knock", 0), (1.85, "clap", 0)], seed=3)
    path = tmp_path / "background.wav"
    sf.write(path, audio, 16000, subtype="FLOAT")
    monkeypatch.setattr(module, "_dialog_availability", lambda: "P03 unavailable in test")
    result = module.evaluate_background(path, load_profile(), classifier=PassThroughClassifier())
    assert result["duration_s"] == pytest.approx(2)
    assert result["false_knocks"] == result["onsets"] > 0
    assert result["false_knocks_per_hour"] == pytest.approx(result["false_knocks"] * 1800)
    assert result["false_confirmations"] is None
    assert "unavailable" in result["dialog_skip_reason"]


def test_background_dialog_counts_confirmations_and_prompt_tail():
    pytest.importorskip("heartbeat.dialog")
    from heartbeat.classify import Detection
    from heartbeat.dialog import PromptKnocks, PromptVoice
    from heartbeat.prompter import FakePrompter
    from tools.eval_background import simulate_dialog_events

    profile = load_profile()
    # Control only the deterministic dialog fixtures; do not invoke hardware.
    profile = replace(profile, dialog={**profile.dialog, "auto_challenge": False, "auto_interview": False,
                                      "first_pattern": 3, "patterns": [2, 3], "p_confirm": 0.0001})
    fake = FakePrompter(cfg=profile.prompter, no_survivor=True, seed=1)
    first_end, replies = fake.schedule_batch([PromptVoice("copy_knocks"), PromptKnocks(3, profile.dialog["interval_s"])], 0)
    assert replies == []
    first_times = [first_end + offset for offset in (1, 1.5, 2)]
    second_start = first_times[-1] + profile.dialog["early_close_s"]
    second_end, _ = fake.schedule_batch([PromptVoice("copy_knocks"), PromptKnocks(2, profile.dialog["interval_s"])], second_start)
    times = sorted([first_end + profile.prompter["gate_tail_s"], *first_times, second_end + 1, second_end + 1.5])
    events = [Detection(t=t, peak=0.8, snr_db=30, gated=False,
                        clip=np.empty(0, dtype=np.float32), score=1.0, is_knock=True,
                        classifier="scripted", top=[]) for t in times]
    observed = []
    result = simulate_dialog_events(events, 59, profile, emit=lambda kind, t, data: observed.append((kind, t, data)))
    assert result["challenge_attempts"] == 1
    assert result["false_confirmations"] == 1
    assert [data["heard"] for kind, t, data in observed if kind == "round_result"] == [3, 2]
    assert result["false_confirmations_per_hour"] == pytest.approx(3600 / 59)
    silence = simulate_dialog_events([], 120, profile)
    assert silence["false_confirmations"] == 0
    assert silence["challenges_started"] == 2


def test_local_head_completes_the_survivor_simulator(cfg):
    if not Path(cfg.head_path).is_file() or not (Path(cfg.model_dir) / "yamnet.tflite").is_file():
        pytest.skip("local head/YAMNet absent; tests never download models")
    pytest.importorskip("sklearn")
    pytest.importorskip("heartbeat.dialog")
    from heartbeat.classifiers.head import HeadClassifier
    from heartbeat.runner import run_simulation

    try:
        HeadClassifier(cfg)
    except RuntimeError as exc:
        if "No CPU TFLite interpreter" in str(exc):
            pytest.skip(str(exc))
        raise
    profile = load_profile()
    result = run_simulation(profile, people=2, can_move="yes", hurt="no", seed=1)
    assert result.snapshot["status"] == "CONFIRMED"
    assert result.snapshot["answers"] == {"q_people": 2, "q_move": "yes", "q_hurt": "no"}
    assert all(event.data["classifier"] == "head" for event in result.events if event.type == "sound")
