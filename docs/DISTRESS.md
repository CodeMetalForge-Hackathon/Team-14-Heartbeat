# Continuous vocal monitoring

This experimental mode runs three-second CLAP windows every second alongside
the existing impact classifier. It reports SCREAM, SHOUT, CRYING, SPEECH, BREATHING, OTHER,
or UNCERTAIN. Vocal events never enter the knock-counting dialog. `hb listen`
and `hb run` do not control motors. In `hb patrol`, this profile enables
automatic stop, settle, fresh-audio verification, and operator review; see
[the rover audio guide](ROVER-AUDIO.md). Possible distress is not confirmation.

From the repository root, start the service in terminal 1:

```bash
uv run --python 3.12 --script tools/clap_service.py --profile profiles/clap-distress.yaml
```

This profile uses port 8767, separate from the original knock-only service on
8766. If the updated service is already running, use it; do not start another
copy on the same port. Add `--allow-download` if model weights are not cached.

In terminal 2, a person starts the microphone listener:

```bash
uv run hb listen --device stick:/dev/cu.usbserial-81520161EE --profile clap-distress --debug
```

Close other listeners using this stick first. Keep the microphone unobstructed.
The first vocal result needs about three seconds of audio plus inference time.
Listening continues until Ctrl+C. Test normal speech, a brief raised voice,
crying-like sounds, knocks, and non-vocal noise separately. Do not strain your
voice. Record which sounds you actually made to evaluate false alerts and misses.
The threshold is experimental; relative scores are not accuracy percentages.

`BREATHING` is informational and never raises a distress alert by itself or
triggers transcription. Its descriptions cover audible breathing and panting;
comparison descriptions include wind, fans, and clothing rustling. In a quiet
room, try normal breathing near the microphone without blowing directly into
it, then compare with those background sounds. No need to strain or hold your
breath. Quiet audio below the existing -50 dBFS window gate is skipped. Breathing
recognition has not been acoustically validated, especially at distance or
through debris; it does not measure respiration rate or medical condition.

After updating descriptions, restart both the CLAP service and listener. Their
description sets must match. Adding categories changes relative scores, so
repeat the speech, scream, and non-vocal checks too.

For the dashboard and manual prompts:

```bash
uv run hb run --device stick:/dev/cu.usbserial-81520161EE --profile clap-distress --knocker manual --dashboard
```

The activity log displays observations and review alerts. Observation metadata
is included in black-box events; the normal full-session raw recording supports
later inspection. Only the deterministic challenge-response engine can decide
CONFIRMED; CLAP observations cannot.

## Optional exact help phrases

The default labels describe sounds. To transcribe actual words, edit
`classifier.clap.monitor.asr.enabled` to `true` in `profiles/clap-distress.yaml`,
then stop and restart both service and listener. On the first service start use:

```bash
uv run --python 3.12 --script tools/clap_service.py --profile profiles/clap-distress.yaml --allow-download
```

This additionally downloads `openai/whisper-tiny.en`. It runs locally in the
optional model service; audio is not sent to a cloud transcription service.
Both client and service must use the same updated profile. The console shows
`Heard:` and `HELP_PHRASE` when normalized whole-word phrases match. English
phrases include help, help me, I am trapped, I'm trapped, and over here. Edit
the phrase list for your test. Transcription runs only on sufficiently loud
windows classified as vocals. Quiet speech and words crossing window boundaries
may be missed; transcripts may be wrong. A phrase match does not infer intent,
negation, urgency, or whether a voice came from a recording.

ASR is off by default and its model was not downloaded during development.
Its integration and phrase matching were tested with fakes; real transcription
accuracy and performance still need checking. It can increase latency for
queued knocks because the service shares one inference worker. If the machine
cannot keep up, disable ASR or move inference to more suitable hardware.

## Coverage and timing

The monitor uses a fixed-size buffer and at most one active plus one pending
window. Knock requests take priority when scheduling new monitor work. If busy,
the latest window replaces older pending work and a status event reports lost
coverage. Inference errors are logged, not converted to detections. A monitor
failure does not turn a vocal event into a knock.

Windows overlapping the system's own prompts and configured gate tail are
discarded; late results from before the gate are suppressed. Ordinary speech
is informational. Screams, shouting, crying, and matched help phrases can raise
a review alert, with a five-second cooldown. The monitor does not establish
that someone needs help; ordinary laughter, media, motors, and distant voices
must be included in field tests. Actual scream/cry recognition, live prompt
gating, and Pi/Jetson performance remain unverified.

All numbers and descriptions are in `profiles/clap-distress.yaml`. The original
`clap` and `rubble` profiles retain their prior behavior. See `CLAP.md` for the
base integration and Pi-to-Mac/Jetson setup.
