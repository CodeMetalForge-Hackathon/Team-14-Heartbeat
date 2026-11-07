# P05: knock classifier (YAMNet + trained head), data tools, measured error rates

Read `AGENTS.md` first. **Lane B, after P01 is committed.** This can run while P03 and P04 are in progress, so touch only your files.

## Goal
This is the AI part, which tells a knock apart from footsteps, drops, claps and speech.

The pitch needs numbers:
- knock recall
- false knocks per hour
- **false confirmations per hour**, which we expect to be 0, because the dialog filters what the classifier misses

## Files you own
- `heartbeat/classifiers/`: a new package with `features.py`, `heuristic.py`, `yamnet.py`, `head.py`, and an `__init__.py` that registers them.
- A one-line import in `heartbeat/classify.py` so the registry loads, if needed.
- `tools/fetch_models.py`, `tools/segment.py`, `tools/label_review.py`, `tools/train_head.py`, `tools/eval_background.py`
- `models/`: only the small trained head and the class map.
- `reports/`
- `tests/test_classifiers.py`
- The `classifier` section of `profiles/rubble.yaml`.
- An optional-dependency group `ml` in `pyproject.toml`: ai-edge-litert, scikit-learn, joblib.

## 1. Fetch the models
`tools/fetch_models.py` downloads files into `models/`:
- **YAMNet TFLite.**
  - Try first: `https://storage.googleapis.com/mediapipe-models/audio_classifier/yamnet/float32/1/yamnet.tflite`
  - If that fails, find the YAMNet TFLite classification model on Kaggle Models (`google/yamnet`).
- **The class map:** `https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv`

Then:
- Verify by running one inference.
- Print the input and output tensor details, including whether an embedding output exists.
- Ask the human to approve network access.

## 2. Classifiers
Each one implements `classify(onset) -> Detection`.

**`features.py`:** DSP features computed from the clip with numpy and scipy only.
- attack time
- decay time to -20 dB
- spectral centroid
- low/mid/high band energy ratios
- peak-to-RMS ratio
- 32 log-mel band means

**`heuristic`**
- No ML dependencies; hand thresholds on the DSP features turn into a score.
- It always works, so it is the fallback.

**`yamnet`**
- Run with ai-edge-litert's `Interpreter`. Fall back to `tflite_runtime` if that is missing.
- Input is the 15600-sample clip.
- The zero-shot score is the max over the target classes. Look them up **by display name** from the class map:
  - Default targets: "Knock", "Tap", "Thump, thud".
  - Print all names containing knock, tap, thump, bang, wood, hammer or slap, so we can choose.
- Return the top 5 labels.

**`head`**
- Features: the YAMNet 521 class scores, plus its embedding if the model exposes one, plus the DSP features.
- Model: StandardScaler, then LogisticRegression with `class_weight="balanced"`.
- Save to `models/knock_head.joblib` with the feature spec, a training-data hash and the chosen threshold.

**`auto`**
- Order of preference: head if `models/knock_head.joblib` exists, then yamnet if the model file exists, then heuristic.
- Log which one is active at startup.
- Set `classifier.name: auto` in the profile.

**Speed:** time inference per clip and print it. The budget is ≤ 30 ms on a Pi 5 CPU.

## 3. Data tools
- **Raw files** are named `data/raw/<label>__<surface>__<desc>__<NN>.wav`, one label per file. Labels: `knock`, `other`, `bg`, `self`. `self` means our own servo or speaker.
- **`tools/segment.py`:**
  - Runs P01's `OnsetDetector` over every raw file.
  - Writes `data/clips/<label>/<stem>__<t>.wav` and `data/clips/index.csv` with columns: path, label, source_file, surface, t, snr_db.
  - `other`, `bg` and `self` are negatives.
- **`tools/label_review.py`:**
  - Writes a static HTML contact sheet per label: audio players, a waveform thumbnail, and the score from the current classifier.
  - Highest-scoring negatives and lowest-scoring knocks come first.
  - A human uses it to delete bad clips, for example a cough inside a knock file.
- **`tools/train_head.py`:**
  - Cross-validation grouped by `source_file`: **never split one recording across train and test.**
  - Report ROC AUC, and precision and recall at the threshold.
  - Report a confusion matrix overall and per surface.
  - Choose the threshold where the false-positive rate on negatives is ≤ 2%, and report recall at that point.
  - Write `reports/classifier.md` comparing **heuristic vs. YAMNet zero-shot vs. head**.
- **`tools/eval_background.py WAV [--classifier X]`** runs on a long venue recording and reports:
  - onsets per hour
  - knock-classified false alarms per hour
  - **simulated false confirmations:** run the `DialogEngine` with a `FakePrompter` that never answers, issuing a challenge every 60 s against the background. Count how often noise alone reaches `CONFIRMED`. If P03 isn't merged yet, skip this part and say so.

  It writes `reports/background_<name>.md`.

## Tests
- `heuristic` separates synthetic knocks from synthetic speech and claps, with AUC > 0.9.
- `head` trains end to end on synthetic data, then saves and loads.
- `yamnet` tests are skipped when the model file is missing.
- The grouped split never puts the same source file in both train and test.
- `auto` falls back correctly.

## Done when
Tests pass. Run the pipeline on whatever real data exists in `data/raw/`:
```
segment → train_head → reports/classifier.md
```
If `data/raw/` is empty, use synthetic data and say so clearly.

Commit `P05: knock classifier + data tools`. Paste the comparison table into your report.

## Notes from the P01 review (read before starting)
- **`ClassifierConfig` is a frozen dataclass with only `name` and `threshold`.** `config.py` rejects any unknown key. You may edit `config.py`, but **only** to add `ClassifierConfig` fields, each with a default, for example:
  - `targets: list[str]`
  - `model_dir: str`
  - `head_path: str`

  Then add the matching keys to `profiles/rubble.yaml`. Keep the other sections untouched.
- **API.** `Detection` subclasses `Onset`. Register each factory with `classify.register(name, factory)`; the factory receives a `ClassifierConfig`.
- **Profile switch.** When you finish, set `classifier.name: auto` in `profiles/rubble.yaml`. P03 relies on it.
