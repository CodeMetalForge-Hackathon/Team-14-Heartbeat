# Approach debris closely before listening — code only

User request: "so now works great thank you bt I need it tto stop as close to
the debris caause thhis distance is very far like let it hit the debris and
then stop to listten (only then stop to listen) just make the code changes asd
of now cause we have disconnected the rrovre"

Follow-up to the stopping-gap question: "make it as close as possible I guess
let the model judge is it the closest to thhe debris or not or it can go closer
make the threshold small I guess or however is good But I need like very very
close to hear the knock please you suggest me some wayss"

Implement a close debris approach policy in pi-explore with a nominal 3 cm
ultrasonic target and small bounded camera-approved moves. Do not start listening
for distant debris or infer physical contact from a missing echo. Preserve normal
exploration guards and motor fault/STOP checks. Actual contact requires a contact
sensor; explain microphone mounting/coupling options, including that the current
M5Stick firmware only supports its built-in PDM microphone. Make code changes and
hardware-free checks only: do not connect, upload firmware, or run a live pipeline.
