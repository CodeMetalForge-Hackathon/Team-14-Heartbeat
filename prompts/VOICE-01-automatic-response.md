# VOICE-01: automatic Pi listen-and-respond behavior

User request:

> distorted tones, and let's actually write the logic of it, or is it already written and works like when we do the actual exploring with rover and it finds someone knocking or screaming and it will say something.

User follow-up:

> please finish this logic, and we will connect the pi to the rover and install the rest of the logic for the automation of rover movement.

Finish automatic stationary voice-response integration using the existing
classifier, dialog engine and prompt worker. Reuse the existing `hb run --auto`
switch and profile settings. Fresh distress observations may request a knock
challenge; they must never count as knock answers or confirm a person. Preserve
self-prompt gating, cooldowns and hardware-error handling. Test without physical
devices or network. Rover movement integration is a later step after connection.
The Grove speaker still has unresolved distortion; do not claim hardware voice
output works based on software tests.
