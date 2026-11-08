# Camera decisions when the ultrasonic sensor has no echo

User request: "so do one thing for this module, if it is returning nothig like
valid =0 and all move the rover as the AI model decides"

Enable camera-directed movement in pi-explore on a fresh, explicit
`RANGE cm=-1 valid=0` result. Keep the measured distance unknown in model input,
events and the dashboard. Preserve valid close-obstacle stops, fresh camera and
sensor transactions, fixed movement arguments, confidence/time/distance limits,
STOP, motor stall checks and watchdogs. Other profiles remain strict by default.
Update both Pi control and guarded MCU motion so the requested mode can work on
the connected rover; expose a capability and acknowledged opt-in policy.

Follow-ups: "hi so will it move forward now to explore?" and "so now please
run the code I want to see the results and it move forward". Finish the firmware
update, run the full live pipeline, and report actual movement and stop reasons.
A live stop/reconnect cleared MCU RAM settings; restore NAVCFG and NOECHO before
the next move and regression-test interrupted motion recovery.
