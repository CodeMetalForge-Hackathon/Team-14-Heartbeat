# CLAP-02: vocal observations and optional help phrases

User asked whether CLAP can recognize screaming, calling for help, and other
sounds people make when they need help. The response proposed separate
continuous overlapping windows, separate event labels, and optional speech
recognition for exact words. User authorized implementation:

> cab you add these changes

Implement alongside the existing CLAP integration. Keep observations separate
from knocks and deterministic confirmation, preserve capture during inference,
and provide an offline-tested path for humans to run with the stick.
