# CLAP-01: streaming integration

User request:

> so now this CLAP firstly waits and then saves audio and only then runs it,
> how i can make it betetr and integrate to the current project instead of yamnet ?

Follow-up:

> how I can test it now ?

The user already tested `laion/clap-htsat-unfused` on a saved five-second
M5Stick recording. Integrate optional CLAP classification with continuous audio
capture and the existing project interfaces. Preserve the deterministic dialog,
timestamps, gating, and hardware controls. Keep the heavy model runtime outside
the core Pi dependency set. Provide commands and offline validation; humans
perform all hardware tests.

During live testing the user provided a log containing only rejected candidates
and clarified that it mixed knocks, bumps, and speech. Add top classifier
descriptions to the existing debug flag and document separate trials; do not
lower the cutoff without labeled positive and negative evidence.
