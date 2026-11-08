# Raspberry Pi CLAP setup request

User requested cloning Team 14 through their authenticated GitHub CLI,
installing the model for knock and vocal detection, and receiving a live
signal stream. They clarified: "no we're not using YAMnet model, we changed
to CLAP model, check the latest commit please".

Scope: verify the latest upstream commit, install the existing isolated CLAP
service for Pi CPU, validate model inference and protocol without hardware,
and provide the human-operated microphone streaming command. Preserve the
existing CLAP configuration, model and interfaces.
