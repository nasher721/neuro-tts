# Planning synthesis

The requested scope is both reviewer UI and a full control center. The current
add-on is a synchronous prototype, so the plan prioritizes safety boundaries
before visual polish: async queue, profile-scoped persistence, main-thread-only
Anki commits, source/configuration digests, and managed-only media ownership.

The UI is organized around Overview, Scope, Engine, Queue, Diagnostics, and
Maintenance. Reviewer controls remain compact and command-oriented, with a
fallback to existing menu/shortcut behavior. F5-TTS remains behind an adapter,
with one worker by default because Apple Silicon MPS generation is resource
intensive.
