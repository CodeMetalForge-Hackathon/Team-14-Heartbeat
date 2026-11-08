# Pi connected hardware check

User request (verbatim):

> tell me where to put the api key, and then let's connect everything and test everything. everything on the hardware side is connected.

Prepare the Pi's ignored `.env.local` for `OPENAI_API_KEY`, without printing or
committing a credential. Identify the actual USB devices, install the matching
Auriga firmware, and check the microphone, camera, local CLAP service, rover
STOP/status/range, GPS, and available speaker output. First motor checks require
the supported chassis with tracks off the ground. API checks depend on the
operator supplying the key locally. Keep the earlier speaker distortion and
GPS no-fix findings visible; a software test is not physical validation.

Operator confirmations during the checks: the key was saved locally, both
tracks were securely lifted with the power switch accessible, the speaker was
still directly on GPIO12, and the ultrasonic target was in place with its cable
in Auriga port 6. Both tracks were seen to turn during the 25% guarded test.
That test timed out; a 35% comparison completed with matching encoder progress.
Record the measured drive setting in the knock profile while retaining all
motion opt-ins and firmware guards.
