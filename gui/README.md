# Dagvane GUI (placeholder)

The native C++20/Qt 6 client arrives in milestone G5b, after G5a delivers and
freezes the versioned command/result NDJSON process protocol. The existing
Council event frames are not that IPC contract. See
`../docs/DEVELOPMENT_PLAN.md` and `../docs/ARCHITECTURE.md`.

No Qt implementation exists in the current repository. The GUI remains a thin
client: provider, orchestration, tool, Goal and Git logic stay in the Python
engine.
