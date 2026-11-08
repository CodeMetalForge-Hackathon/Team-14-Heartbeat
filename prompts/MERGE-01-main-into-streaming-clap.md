# Merge latest main into streaming-clap

User request (verbatim):

> can you pull the latest changes from main and resolve the conflicts in the current brancg ?

Merge origin/main at 7db6d25 into streaming-clap, preserving both branches' work.
Resolve the competing rover startup implementations in favor of main's single
PONG waiter and READY-aware retry loop. Retain guarded movement capability checks,
STOP behavior and the existing legacy timing fields; boot_timeout_s and
boot_ping_interval_s control startup after the merge.

Retain incoming voice-response, GPS and CPU-only Pi CLAP changes, and reconcile
the Decisions deployment guide with the newly available local CLAP helper.
Do not modify unrelated .idea files or include ignored .env.local credentials.

The user's earlier instruction not to create or run tests remains in effect.
Validate source syntax, configuration syntax and merge completeness without
opening hardware, running tests, or making inference requests.
