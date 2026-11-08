# PI-05: move recovery to a new Pi 5

Operator request: push all current changes and errors to GitHub, with
instructions for the Codex session on the replacement Raspberry Pi 5.
The new Pi has an NVMe M.2 HAT+, rather than the old Pi's reported AI HAT.

Read `AGENTS.md` and `docs/NEW-PI-HANDOFF.md`, then follow the recovery and
setup sequence there. Read the linked PI-04 prompt and evidence before opening
hardware. Do not assume the old host's paths or physical setup remain valid.

The handoff task owns documentation, its README link, and the preserved
firmware artifact under `docs/evidence/rover-recovery/`. It does not change
application code, profiles, firmware source, credentials or boot settings.
The operator explicitly requested committing/pushing the incomplete recovery
record now so the new session can continue. Preserve the fact that recovery
and full pipeline validation remain pending; migration is not evidence of a fix.

On the new host, prepare dependencies, run the offline suite once, perform
PI-04's bounded recovery steps with the operator, then run one supervised full
pipeline test when the hardware gates pass. Record results and push them.
