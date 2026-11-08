# Restore pre-Decisions rover exploration

User request (verbatim):

> can be revert back to the point where no decision api was there and it run forward for like a minute ?

Restore the exploration behavior from 556c822 by reversing the Decisions API
feature introduced in 6eb3bed. Preserve the subsequent main merge's USB boot
handshake, stationary voice response, GPS tools and CPU-only Pi CLAP setup.
Keep every historical prompt and the .env.local ignore rule; leave unrelated
.idea files untouched. Remove the camera/Decisions preview and runtime code,
configuration and obsolete deployment guide.

Set the default exploration budget to 60 seconds. Keep unknown-range handling,
audio interruption, encoder/stall guards,
watchdog, STOP and E-STOP behavior. This is not a blind timed motor command and
does not establish a successful one-minute hardware run or fix existing faults.

Subsequent steering (verbatim):

> or can it just use the ultrasonic for  obstacle detection and if the obstacle is close then it should stop and if it's far  and there's no obstacle it should run

Use zero NAVCFG turn angle to select forward-only obstacle waiting, as the new
default. Keep the 30 cm stop / 45 cm restart thresholds and require two valid
clear readings after an obstacle. Never turn or reverse in this mode. Poll while
stopped; blocked waiting counts toward the same 60-second budget. Unknown range
stays stopped and retains the existing bounded recovery/abort behavior. An
explicit positive turn angle keeps the previous search-turn behavior. Older
firmware must reject zero-angle configuration without an unguarded fallback.

Record and publish the rollback as a new commit on streaming-clap, preserving
history. The user's instruction not to create or run tests remains in force.
Use source/syntax checks and compile the sketch; do not operate hardware or upload firmware.
