# Background evaluation: synthetic_background

**Synthetic fixture only. These results do not measure venue or hardware performance.**

Input: `/private/tmp/heartbeat-p05-data/synthetic_background.wav`. Duration: 600.000000 s (0.166667 h). Requested classifier: `auto`. Active classifier(s): head.

| Measure | Count | Rate per hour |
|---|---:|---:|
| Onsets | 562 | 3372.000000 |
| Knock-classified false alarms | 32 | 192.000000 |
| Simulated false confirmations | 0 | 0.000000 |

The actual P03 DialogEngine was challenged every 60 s, starting at t=0: 10 attempts, 10 started, 0 rejected while busy. FakePrompter(no_survivor=True) scheduled 27 prompt batches with their actual virtual durations and injected no answers. Prompt completion, inclusive gate tails, audio onsets, and response deadlines were replayed in stream-time order. Each distinct transition into CONFIRMED counts once; 0 confirmed trials were reset before a later scheduled challenge. Final status: NO_SIGNS.

The input is assumed to be background only, with no deliberate human responses. All knock classifications are therefore counted as false alarms. Audio was streamed through WavSource and OnsetDetector, including the terminal flush, using sample time and no microphone, speaker, serial port, or real-time sleeps. Per-hour rates are extrapolated from this recording's duration; a zero count does not establish a zero long-term error rate.
