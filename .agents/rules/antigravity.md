---
trigger: always_on
---

# Skyrim toolkit: notes for Google Antigravity

- Antigravity cuts every instruction file at 24,000 bytes. `AGENTS.md` is longer: its safety rules, saves and file
  locks all come before the cut, but the sections from "Confidence Levels (Mandatory)" to the end may not reach you,
  or only in part (setup writes your install's paths into the top of the file, which moves the cut). Read them from `AGENTS.md` with your file tool at the start of a session.
- `AGENTS.md` says "Codex" in places; where a rule names a host, read it as applying to you unless it is about Codex
  configuration files.
- The toolkit's guard runs as your PreToolUse hook only if `.agents/hooks.json` registers it
  (`python tools/adapters/antigravity/install.py --apply`). It has passed the toolkit's offline conformance checks
  but has NOT been proven on a live Antigravity session, and what Antigravity does when a hook fails or times out
  is undocumented. Until the live canaries in `ADAPTING.md` section 7 pass, do not rely on it: ask the user before
  any change to the game folder, as if no guard ran.
- Details: `docs/antigravity.md`.
