# Standalone distance sensor check

User request: "hi dont test anyting else right now maybe make a separate test
file to just test this distance sensor"

Create a separate, manually invoked distance sensor script for the rover.
Show valid distances and missing echoes, use the configured ultrasonic port,
keep motors stopped, and leave the camera, microphone, dashboard and AI pipeline
out of the check. Do not run the script, hardware checks or automated tests.
