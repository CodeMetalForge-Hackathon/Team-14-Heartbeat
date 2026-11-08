User requested a full Decisions API rover test with movement, knocks and other audio signals, then specified 50 cm travel. User confirms tabletop testing. Prepare an operator-run guarded forward test with a cumulative 50 cm command cap, no turns, vocal monitoring, existing sensor/STOP guards, and saved evidence. Do not claim calibrated travel or complete physical validation.

Follow-up: user reported continuous HOLD/low_confidence and requested progress when no knocks are heard. Inspect the saved evidence, update the test mission to favor supported forward progress, distinguish model HOLD from confidence-blocked movement, and fix the observed vocal-window starvation during rapid navigation transitions. Preserve obstacle, edge-awareness, confidence, STOP and distance-budget constraints.

Follow-up: user requested lowering the full-check motion confidence threshold
from 0.70 to 0.50, running the full pipeline again, and committing and pushing
all accumulated changes after completion.

Follow-up: user requested smooth movement without the routine stops every 3 cm.
Implement continuous guarded forward travel with camera/API and audio checks
while moving. Retain the existing total travel limit and meaningful stop causes.
User asked to avoid excessive testing; focus checks on the changed movement path.

Follow-up: after failed uploads, invalid serial replies and unexpected left-track
movement, user requested: "can you push all the latest changes and errors that
we're currently facing". Record observed failures, current deployment status,
validation limits and unresolved questions, then commit and push. Do not restart
hardware testing while preparing the record.
