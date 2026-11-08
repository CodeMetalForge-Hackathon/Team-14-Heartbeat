# GPS-01: check the connected NEO-M9N

User request:

> i also connected a blox neo M9N gps device, can u please test that out as well if it's working well.

User follow-up:

> and then after u finish everything please commit and push to github your changes.

Test the connected receiver without changing its configuration. Distinguish
USB/serial communication, valid GNSS messages, satellite acquisition and a
usable position fix. Keep raw position recordings out of the commit. Provide
a bounded repeatable diagnostic and hardware-free checks for its parsing and
fix validity. This task does not implement GPS navigation or rover movement.

During diagnosis the operator confirmed no antenna was attached and asked
whether one was needed. Explain the requirement and distinguish a possible
onboard carrier antenna from an external antenna; do not claim positioning
works from USB enumeration or valid no-fix messages.

Follow-up antenna test request:

> it's the ANN-MB5-00-00, i did connect it with that SMA to UFL adapter cable, test it pls

The operator subsequently confirmed the antenna was indoors away from a
window and could not move it during the test. Record measured reception and
fix status without claiming that an indoor no-fix result validates or rules
out an electrical antenna problem.

Window-position retest request:

> i moved closer to the window test once more pls
