# User request
Help fix missed knocks and run the full pipeline successfully. Do not make tests.
User confirmed the rover is connected, a one-metre path is clear, and requested starting it.

# Change
Add a configurable CLAP candidate cutoff for MOVING only. Use 0.50 in clap-knocks based on recorded 0.5036 and 0.5541 scores. Preserve 0.60 stationary verification/dialog classification, freshness and prompt gates, STOP acknowledgement and firmware stall guards. This is an initial sensitivity adjustment, not validated calibration or a fix for encoder faults.
