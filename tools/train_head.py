#!/usr/bin/env python3
"""Train/evaluate a knock head with recording-disjoint fit/calibration/test sets.

Example: uv run --extra ml python tools/train_head.py --profile rubble
No audio devices or network are accessed. A real YAMNet is required by default;
--feature-mode dsp explicitly produces a separately identified DSP-only head.
YAMNet class-score scales have a configured lower bound so rare activations do
not explode under standardization; thresholds remain independently calibrated.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import platform
import sys
from time import perf_counter

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import soundfile as sf

from heartbeat.config import load_profile
from heartbeat.classifiers.head import (
    CLIP_SAMPLES, SAMPLE_RATE, YAMNET_CLASSES, FeatureExtractor, save_head,
)
from heartbeat.classifiers.features import extract_features, feature_names, settings
from tools.segment import read_index, resolve_index_path


class ThresholdUnattainableError(ValueError):
    """Inclusive scores cannot meet the calibration FPR with a [0,1] threshold."""


def _training(cfg):
    return settings(cfg, "training")


def _labels_groups(labels, source_files):
    y = np.asarray(labels, dtype=int)
    groups = np.asarray(source_files, dtype=str)
    if (y.ndim != 1 or groups.ndim != 1 or len(y) != len(groups) or not len(y) or
            not set(np.unique(y)).issubset({0, 1}) or np.unique(y).size != 2):
        raise ValueError("labels/groups must be equal-length vectors containing both binary classes")
    if any(not group.strip() for group in groups):
        raise ValueError("every clip needs a nonempty source_file recording group")
    return y, groups


def _group_labels(y, groups):
    unique = np.unique(groups)
    labels = []
    for group in unique:
        values = np.unique(y[groups == group])
        if len(values) != 1:
            raise ValueError(f"source recording {group!r} has conflicting labels; one label per raw file is required")
        labels.append(int(values[0]))
    return unique, np.asarray(labels, dtype=int)


def grouped_splits(labels, source_files, n_splits, random_seed):
    """Return stratified CV folds, assigning whole recordings to one side only.

    Stratification operates on source files, so duplicate/nearby clips within a
    recording cannot dominate fold assignment or cross the training boundary.
    """
    from sklearn.model_selection import StratifiedKFold
    y, groups = _labels_groups(labels, source_files)
    unique, group_y = _group_labels(y, groups)
    split_count = min(int(n_splits), int(np.bincount(group_y, minlength=2).min()))
    if split_count < 2:
        raise ValueError("grouped CV requires at least two source recordings of each class after calibration holdout")
    splitter = StratifiedKFold(n_splits=split_count, shuffle=True,
                              random_state=int(random_seed))
    result = []
    for train_groups, test_groups in splitter.split(unique, group_y):
        train = np.flatnonzero(np.isin(groups, unique[train_groups]))
        test = np.flatnonzero(np.isin(groups, unique[test_groups]))
        result.append((train, test))
    return result


def calibration_split(labels, source_files, fraction, random_seed):
    """Reserve recordings per class; leave at least two per class for CV.

    The requested fraction is rounded to whole recordings within each class.
    At least one class-specific recording is reserved, even in small datasets.
    """
    y, groups = _labels_groups(labels, source_files)
    unique, group_y = _group_labels(y, groups)
    if not 0 < float(fraction) < 1:
        raise ValueError("calibration_fraction must lie strictly between 0 and 1")
    rng = np.random.default_rng(int(random_seed))
    calibration_groups = []
    for label in (0, 1):
        candidates = unique[group_y == label].copy()
        if len(candidates) < 3:
            raise ValueError("honest calibration plus grouped assessment needs at least three source recordings per class")
        rng.shuffle(candidates)
        count = min(len(candidates) - 2, max(1, round(len(candidates) * float(fraction))))
        calibration_groups.extend(candidates[:count].tolist())
    calibration = np.flatnonzero(np.isin(groups, calibration_groups))
    development = np.flatnonzero(~np.isin(groups, calibration_groups))
    return development, calibration


def choose_threshold(labels, scores, max_false_positive_rate):
    """Lowest inclusive score threshold meeting the empirical negative FPR.

    Ties at the excluded negative score are all excluded. This can be stricter
    than the requested rate; it never resolves a tie using the positive labels.
    """
    y = np.asarray(labels, dtype=int)
    scores = np.asarray(scores, dtype=float)
    maximum = float(max_false_positive_rate)
    if (y.shape != scores.shape or y.ndim != 1 or not len(y) or
            not np.all(np.isfinite(scores)) or np.any((scores < 0) | (scores > 1)) or
            not set(np.unique(y)).issubset({0, 1}) or not 0 <= maximum <= 1):
        raise ValueError("threshold calibration requires finite [0,1] scores, binary labels and FPR in [0,1]")
    negatives = np.sort(scores[y == 0])[::-1]
    if not len(negatives):
        raise ValueError("threshold calibration requires negative examples")
    allowed = math.floor(maximum * len(negatives))
    if allowed >= len(negatives):
        return 0.0
    threshold = float(np.nextafter(negatives[allowed], np.inf))
    if threshold > 1:
        raise ThresholdUnattainableError(
            "no threshold in [0,1] meets the requested calibration FPR: a negative score is saturated at 1")
    return threshold


def fit_head(features, labels, cfg):
    """Fit StandardScaler -> balanced LogisticRegression with robust score scales.

    The scaler sees only this fit's training rows. For full YAMNet/DSP vectors,
    its first 521 scaling denominators are floored at score_scale_floor before
    fitting logistic regression. Near-constant AudioSet scores therefore cannot
    amplify a small unseen activation arbitrarily. Embedding and DSP scales are
    unchanged; DSP-only and small numeric test matrices use ordinary scaling.
    """
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    settings = _training(cfg)
    X = np.asarray(features, dtype=float)
    y = np.asarray(labels, dtype=int)
    if (X.ndim != 2 or X.shape[0] != len(y) or X.shape[1] == 0 or
            not np.all(np.isfinite(X)) or not np.array_equal(np.unique(y), [0, 1])):
        raise ValueError("head fit requires a finite feature matrix and both binary classes")
    scaler = StandardScaler().fit(X)
    if X.shape[1] >= YAMNET_CLASSES + len(feature_names(cfg)):
        floor = float(settings["score_scale_floor"])
        if not math.isfinite(floor) or floor <= 0:
            raise ValueError("score_scale_floor must be finite and positive")
        scaler.scale_[:YAMNET_CLASSES] = np.maximum(scaler.scale_[:YAMNET_CLASSES], floor)
    logistic = LogisticRegression(
        class_weight="balanced", C=float(settings["logistic_c"]),
        max_iter=int(settings["logistic_max_iter"]),
        random_state=int(settings["random_seed"]),
    ).fit(scaler.transform(X), y)
    return Pipeline([("scaler", scaler), ("logistic", logistic)])


def _metrics(labels, scores, predictions):
    from sklearn.metrics import confusion_matrix, precision_score, recall_score, roc_auc_score
    y = np.asarray(labels, dtype=int)
    scores = np.asarray(scores, dtype=float)
    predictions = np.asarray(predictions, dtype=bool)
    matrix = confusion_matrix(y, predictions, labels=[0, 1])
    tn, fp, fn, tp = (int(value) for value in matrix.ravel())
    return {
        "n": int(len(y)), "tn": tn, "fp": fp, "fn": fn, "tp": tp,
        "confusion_matrix": matrix.tolist(),
        "precision": float(precision_score(y, predictions, zero_division=0)),
        "recall": float(recall_score(y, predictions, zero_division=0)),
        "roc_auc": float(roc_auc_score(y, scores)) if np.unique(y).size == 2 else None,
        "false_positive_rate": float(fp / (tn + fp)) if tn + fp else None,
    }


def _summary(labels, scores, predictions, surfaces):
    result = _metrics(labels, scores, predictions)
    result["per_surface"] = {}
    surfaces = np.asarray(surfaces, dtype=str)
    for surface in np.unique(surfaces):
        mask = surfaces == surface
        result["per_surface"][str(surface)] = _metrics(
            np.asarray(labels)[mask], np.asarray(scores)[mask], np.asarray(predictions)[mask])
    return result


def train_and_evaluate(features, labels, source_files, surfaces, cfg, *, baseline_scores=None):
    """Return (final_pipeline, calibrated_threshold, inspectable_report_dict).

    A shared source-disjoint calibration set chooses every threshold. CV test
    groups are never used for fitting or choosing their fold's threshold. The
    final pipeline also excludes the calibration recordings.
    """
    X = np.asarray(features, dtype=float)
    y, groups = _labels_groups(labels, source_files)
    surfaces = np.asarray(surfaces, dtype=str)
    if X.ndim != 2 or len(X) != len(y) or len(surfaces) != len(y):
        raise ValueError("features, labels, groups and surfaces must describe the same clips")
    settings = _training(cfg)
    maximum = float(settings["max_false_positive_rate"])
    development, calibration = calibration_split(
        y, groups, settings["calibration_fraction"], settings["random_seed"])
    folds = grouped_splits(y[development], groups[development],
                           settings["cv_splits"], settings["random_seed"])
    scores = np.full(len(development), np.nan)
    predictions = np.zeros(len(development), dtype=bool)
    fold_reports = []
    for number, (relative_train, relative_test) in enumerate(folds, start=1):
        train, test = development[relative_train], development[relative_test]
        model = fit_head(X[train], y[train], cfg)
        calibration_scores = model.predict_proba(X[calibration])[:, 1]
        threshold = choose_threshold(y[calibration], calibration_scores, maximum)
        test_scores = model.predict_proba(X[test])[:, 1]
        scores[relative_test] = test_scores
        predictions[relative_test] = test_scores >= threshold
        fold_reports.append({
            "fold": number, "threshold": threshold,
            "train_groups": sorted(set(groups[train])),
            "calibration_groups": sorted(set(groups[calibration])),
            "test_groups": sorted(set(groups[test])),
            "calibration": _metrics(y[calibration], calibration_scores,
                                    calibration_scores >= threshold),
        })
    if not np.all(np.isfinite(scores)):
        raise ValueError("grouped CV did not score every assessment example exactly once")
    comparisons = {"head": _summary(y[development], scores, predictions,
                                     surfaces[development])}
    comparisons["head"]["threshold"] = None  # Every CV fold has its own calibrated threshold.
    for name, baseline in (baseline_scores or {}).items():
        baseline = np.asarray(baseline, dtype=float)
        if baseline.shape != y.shape:
            raise ValueError(f"{name} baseline score count differs from the dataset")
        threshold_note = None
        try:
            threshold = choose_threshold(y[calibration], baseline[calibration], maximum)
        except ThresholdUnattainableError as exc:
            threshold = float(cfg.threshold)
            threshold_note = f"{exc}; comparison uses profile threshold {threshold:.17g}"
        comparisons[name] = _summary(y[development], baseline[development],
                                     baseline[development] >= threshold, surfaces[development])
        comparisons[name]["threshold"] = threshold
        comparisons[name]["calibration_constraint_met"] = threshold_note is None
        comparisons[name]["threshold_note"] = threshold_note
        comparisons[name]["calibration"] = _metrics(
            y[calibration], baseline[calibration], baseline[calibration] >= threshold)
        comparisons[name]["profile_threshold"] = float(cfg.threshold)
        comparisons[name]["profile_threshold_metrics"] = _summary(
            y[development], baseline[development], baseline[development] >= float(cfg.threshold),
            surfaces[development])
    final = fit_head(X[development], y[development], cfg)
    final_calibration = final.predict_proba(X[calibration])[:, 1]
    threshold = choose_threshold(y[calibration], final_calibration, maximum)
    report = {
        "training_settings": settings,
        "development_groups": sorted(set(groups[development])),
        "calibration_groups": sorted(set(groups[calibration])),
        "development_indices": development.tolist(),
        "calibration_indices": calibration.tolist(),
        "development_clips": int(len(development)),
        "calibration_clips": int(len(calibration)),
        "calibration_negatives": int(np.sum(y[calibration] == 0)),
        "assessment_scores": scores.tolist(),
        "folds": fold_reports, "comparisons": comparisons,
        "final_threshold": threshold,
        "profile_threshold_at_training": float(cfg.threshold),
        "final_calibration": _metrics(y[calibration], final_calibration,
                                      final_calibration >= threshold),
    }
    return final, threshold, report


def _display(value):
    return "n/a" if value is None else f"{value:.4f}"


def write_report(path, report):
    """Write the measured comparison and disclose the data/assessment limits."""
    comparisons = report["comparisons"]
    maximum = report["training_settings"]["max_false_positive_rate"]
    mode = report["feature_spec"]["mode"]
    lines = [
        "# Knock classifier evaluation", "",
        f"Data provenance: **{report['provenance']}**", "",
        f"Feature mode: **{mode}**. Training-data SHA-256: `{report['training_data_hash']}`.",
        f"Input: `{report['index_path']}`; {report['accepted_clips']} accepted clips, "
        f"{report['missing_clips']} missing indexed clips excluded.",
        f"Labels: `{json.dumps(report['label_counts'], sort_keys=True)}`. "
        f"Surfaces: `{json.dumps(report['surface_counts'], sort_keys=True)}`.", "",
        "## Recording-disjoint assessment", "",
        f"Reserved {len(report['calibration_groups'])} recording groups "
        f"({report['calibration_clips']} clips, {report['calibration_negatives']} negatives) "
        "solely for threshold calibration. These recordings are excluded from every fit and assessment fold.",
        f"The remaining {len(report['development_groups'])} groups "
        f"({report['development_clips']} clips) use {len(report['folds'])}-fold grouped CV. "
        "No recording appears on both sides of a split. Each head test fold uses a model fit on its training "
        "groups and a threshold selected using only the reserved calibration recordings.",
        "The shipped pipeline is fitted on all development groups; its threshold is selected using the same "
        "reserved calibration recordings. Calibration recordings are not folded back into that fit.", "",
        f"Thresholds target empirical negative FPR ≤ {maximum:.2%} on calibration only. "
        "Assessment FPR may be higher. A small, correlated calibration sample does not establish a population "
        "or venue false-alarm guarantee; source-group counts matter more than the number of nearby clips.",
        "Synthetic data validates plumbing and controlled discrimination only. Real contact recordings, "
        "venue background, and Pi 5 latency require separate validation. Clip-level FPR cannot establish "
        "false knocks or false confirmations per hour; use tools/eval_background.py on a long recording.", "",
        "## Comparison on held-out recording folds", "",
        "Baseline thresholds are selected on the reserved calibration recordings when attainable. "
        "A baseline with saturated negative scores that cannot meet the target uses the profile threshold "
        "and is explicitly identified below. The head aggregates separately calibrated fold models.", "",
        "| Classifier | ROC AUC | Precision | Recall | Negative FPR | TN | FP | FN | TP |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("heuristic", "yamnet", "head"):
        if name not in comparisons:
            reason = report.get("unavailable", {}).get(name, "unavailable")
            lines.append(f"| {name} | unavailable: {reason.replace('|', '/')} | n/a | n/a | n/a | n/a | n/a | n/a | n/a |")
            continue
        value = comparisons[name]
        label = "head (DSP only)" if name == "head" and mode == "dsp" else name
        lines.append(f"| {label} | {_display(value['roc_auc'])} | {_display(value['precision'])} | "
                     f"{_display(value['recall'])} | {_display(value['false_positive_rate'])} | "
                     f"{value['tn']} | {value['fp']} | {value['fn']} | {value['tp']} |")
    lines.append("")
    for name, value in comparisons.items():
        if value.get("threshold_note"):
            lines.append(f"- **{name}: calibration FPR target unattainable.** {value['threshold_note']}.")
        elif name != "head":
            lines.append(f"- {name}: calibrated comparison threshold `{value['threshold']:.17g}`.")
    lines.extend(["", "Confusion matrices use rows = actual `[other, knock]`, columns = predicted `[other, knock]`.", "",
                  "## Per-surface confusion matrices", "",
                  "| Classifier | Surface | TN | FP | FN | TP | Precision | Recall | ROC AUC |",
                  "|---|---|---:|---:|---:|---:|---:|---:|---:|"])
    for name, result in comparisons.items():
        for surface, value in result["per_surface"].items():
            lines.append(f"| {name} | {surface.replace('|', '/')} | {value['tn']} | {value['fp']} | "
                         f"{value['fn']} | {value['tp']} | {_display(value['precision'])} | "
                         f"{_display(value['recall'])} | {_display(value['roc_auc'])} |")
    lines.extend(["", "## Saved head and speed", "",
                  f"Artifact: `{report['output_path']}`. Final calibrated threshold: `{report['final_threshold']:.17g}`.",
                  f"Suggested profile setting: `classifier.threshold: {report['final_threshold']:.17g}`. "
                  "Runtime honors the profile threshold; the artifact threshold is a saved calibration suggestion. "
                  f"The profile threshold during this evaluation was `{report['profile_threshold_at_training']:.17g}`.",
                  f"Final calibration confusion matrix: `{report['final_calibration']['confusion_matrix']}`; "
                  f"FPR {_display(report['final_calibration']['false_positive_rate'])}, "
                  f"recall {_display(report['final_calibration']['recall'])}.",
                  "Head: StandardScaler followed by LogisticRegression(class_weight=balanced). "
                  "Feature order/settings and model fingerprint are saved with the pipeline.",
                  (f"The first {YAMNET_CLASSES} YAMNet class-score scaling denominators are floored at "
                   f"`{report['training_settings']['score_scale_floor']}` so rare activations are not "
                   "magnified by near-zero sample variance. Scaling statistics are fit only on each "
                   "training partition; embedding and DSP scales are unchanged. Threshold calibration "
                   "still uses the reserved recording groups."
                   if mode == "yamnet_dsp" else
                   "YAMNet score-scale flooring does not apply to this DSP-only artifact."),
                  f"Measurement host: {report['host']}; {report['speed_repeats']} sequential warm inference clips, CPU.", "",
                  "| Classifier | Mean inference ms/clip |", "|---|---:|"])
    for name in ("heuristic", "yamnet", "head"):
        value = report["inference_ms"].get(name)
        lines.append(f"| {name} | {'unavailable' if value is None else f'{value:.3f}'} |")
    lines.extend(["", "The deployment budget is ≤30 ms/clip on a Pi 5 CPU. Host timings above do not verify that budget.", "",
                  "## Split audit", "",
                  f"Calibration groups: `{json.dumps(report['calibration_groups'])}`.",
                  "Whole recordings share a group even when they contain multiple clips. "
                  "Repeated folds share calibration data, so fold results are dependent.", ""])
    for fold in report["folds"]:
        lines.extend([f"- Fold {fold['fold']}, threshold `{fold['threshold']:.17g}`: "
                      f"fit `{json.dumps(fold['train_groups'])}`; "
                      f"test `{json.dumps(fold['test_groups'])}`."])
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def train_index(index_path, cfg, *, output="models/knock_head.joblib",
                report_path="reports/classifier.md", feature_mode=None, yamnet=None,
                provenance="Unspecified recordings; provenance and labels need human review"):
    """Train from the segmenter index; injectable YAMNet keeps tests offline."""
    from heartbeat.classifiers.heuristic import HeuristicClassifier
    from heartbeat.classifiers.yamnet import YamnetClassifier
    settings = _training(cfg)
    mode = settings["feature_mode"] if feature_mode is None else feature_mode
    index_path = Path(index_path)
    rows = read_index(index_path)
    accepted, clips = [], []
    digest = hashlib.sha256()
    missing = 0
    for row in rows:
        path = resolve_index_path(row, index_path)
        if not path.is_file():
            missing += 1
            continue
        clip, sample_rate = sf.read(path, dtype="float32")
        if sample_rate != SAMPLE_RATE or clip.ndim != 1 or len(clip) != CLIP_SAMPLES:
            raise ValueError(f"{path}: clips must be mono 16000 Hz / 15600 samples; rerun tools/segment.py")
        if not np.all(np.isfinite(clip)):
            raise ValueError(f"{path}: clip contains nonfinite samples")
        digest.update(json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        accepted.append(row)
        clips.append(clip)
    if not accepted:
        raise ValueError("no existing clips in index; segment labeled recordings first")
    heuristic = HeuristicClassifier(cfg)
    unavailable = {}
    if yamnet is None:
        try:
            yamnet = YamnetClassifier(cfg)
        except Exception as exc:
            if mode == "yamnet_dsp":
                raise ValueError(f"YAMNet required for full head features: {exc}. "
                                 "Fetch/verify the real model, or explicitly use --feature-mode dsp.") from exc
            unavailable["yamnet"] = f"{type(exc).__name__}: {exc}"
    extractor = FeatureExtractor(cfg, mode=mode, yamnet=yamnet)
    vectors, heuristic_scores, yamnet_scores = [], [], []
    dsp_size = len(extractor.spec["dsp_names"])
    for clip in clips:
        features = extractor.transform(clip)
        vectors.append(features)
        heuristic_scores.append(heuristic.score_features(features[-dsp_size:]))
        if yamnet is not None:
            scores = features[:521] if mode == "yamnet_dsp" else yamnet.extract(clip)[0]
            yamnet_scores.append(yamnet.score_scores(scores))
    baselines = {"heuristic": np.asarray(heuristic_scores)}
    if yamnet is not None:
        baselines["yamnet"] = np.asarray(yamnet_scores)
    y = np.asarray([row["label"] == "knock" for row in accepted], dtype=int)
    model, threshold, report = train_and_evaluate(
        np.vstack(vectors), y, [row["source_file"] for row in accepted],
        [row["surface"] for row in accepted], cfg, baseline_scores=baselines)
    inference_ms = {}
    repetitions = int(settings["speed_repeats"])
    if repetitions < 1:
        raise ValueError("speed_repeats must be positive")
    for name in ("heuristic", "yamnet", "head"):
        if name == "yamnet" and yamnet is None:
            inference_ms[name] = None
            continue
        # Warm each path before measuring, then cycle through actual indexed clips.
        elapsed = []
        for number in range(repetitions + 1):
            clip = clips[number % len(clips)]
            started = perf_counter()
            if name == "heuristic":
                heuristic.score_features(extract_features(clip, cfg))
            elif name == "yamnet":
                yamnet.score_scores(yamnet.extract(clip)[0])
            else:
                model.predict_proba(extractor.transform(clip).reshape(1, -1))
            if number:
                elapsed.append((perf_counter() - started) * 1000)
        inference_ms[name] = float(np.mean(elapsed))
    report.update({
        "feature_spec": extractor.spec, "training_data_hash": digest.hexdigest(),
        "accepted_clips": len(accepted), "missing_clips": missing,
        "label_counts": dict(Counter(row["label"] for row in accepted)),
        "surface_counts": dict(Counter(row["surface"] for row in accepted)),
        "index_path": str(index_path), "output_path": str(output),
        "provenance": str(provenance), "unavailable": unavailable,
        "inference_ms": inference_ms, "speed_repeats": repetitions,
        "host": f"{platform.system()} {platform.machine()}, Python {platform.python_version()}",
    })
    save_head(output, model, extractor.spec, digest.hexdigest(), threshold, metadata={
        "provenance": report["provenance"],
        "calibration_groups": report["calibration_groups"],
        "development_groups": report["development_groups"],
        "training_settings": settings,
        "assessment": report["comparisons"]["head"],
        "suggested_profile_threshold": threshold,
    })
    write_report(report_path, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", default="data/clips/index.csv")
    parser.add_argument("--profile", default="rubble")
    parser.add_argument("--output", default="models/knock_head.joblib")
    parser.add_argument("--report", default="reports/classifier.md")
    parser.add_argument("--feature-mode", choices=("yamnet_dsp", "dsp"), default=None,
                        help="dsp explicitly omits YAMNet from head features; default comes from profile")
    parser.add_argument("--provenance", default="Unspecified recordings; provenance and labels need human review")
    args = parser.parse_args(argv)
    try:
        report = train_index(args.index, load_profile(args.profile).classifier,
                             output=args.output, report_path=args.report,
                             feature_mode=args.feature_mode, provenance=args.provenance)
    except (OSError, ValueError, ImportError) as exc:
        parser.exit(2, f"training failed: {exc}\n")
    print(f"Saved {args.output} ({report['feature_spec']['mode']}), threshold={report['final_threshold']:.6f}")
    print(f"Suggested profile setting: classifier.threshold: {report['final_threshold']:.17g}")
    print(f"Report: {args.report}; {report['missing_clips']} missing indexed clips excluded")
    for name, ms in report["inference_ms"].items():
        print(f"{name}: {'unavailable' if ms is None else f'{ms:.3f} ms/clip'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
