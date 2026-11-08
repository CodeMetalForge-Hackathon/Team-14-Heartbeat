# PI-06: camera-guided autonomous exploration

The operator requested exploration instead of repeatedly aiming the ultrasonic
sensor at a flat diagnostic target:

> why shall we aim at a falt object as I said it SHOULD EXPLORE make it like that edit what ever you want however you want NOW

Earlier clarification requested ordinary obstacle avoidance, plus stopping to
inspect visible debris irrespective of its ultrasonic distance, while retaining
the full camera, ultrasonic, audio, Decisions API and dashboard pipeline.

Implement bounded forward exploration with turns after an acknowledged STOP and
fresh observations, and a camera mission that requests STOP_AND_LISTEN for
visible debris. An ordinary obstacle stop should allow another observation and
a new direction. A canonical no-echo reading should keep perception active and
retry while stopped. Unknown distance must never authorize a movement. Keep
STOP, sensor, camera, freshness, encoder, confidence, time and distance guards.
Record what has actually been verified on the live rover separately from fake
motor/controller tests.

Follow-up: the operator reported that the battery is new and the rover did make
one forward step. They requested readable reasons in the terminal and dashboard
for each stop, such as debris detection or an obstacle requiring another route.
Distinguish an explicit visual debris choice from generic HOLD, confidence,
audio, sensor and operator stop reasons. Do not claim that HOLD proves debris.
