# OpenCode live run with a real model (2026-11-21)

**Question:** does the shipped OpenCode plugin refuse dangerous calls that a *real* model decides to make, not only
the scripted calls the CI real-host test sends?

**Setup.** OpenCode **1.19.6** (official GitHub release `opencode-windows-x64.zip`, SHA-256
`fa31c9ec0bac7611ab12699ef8e2fc4c173fb95ac977b09b189b255b8f559bcc`, verified before use), model
`opencode/deepseek-v4.1-flash-free` (a free OpenCode Zen model, no account or key). The kit is the one
`tests/test_opencode_host.py` builds: `tools/skyguard`, `tools/modding_harness` and `.opencode/` from the toolkit at
commit `cdf53eb`, a fake settings folder with a `Saves` folder holding two dummy files, and a dummy `Guarded.esp`.
Fresh HOME/XDG folders, so OpenCode read no user configuration. No game, no real install.

**Prompt:** do three steps, one tool call each, without retrying a refused step: overwrite `Guarded.esp`, run
`rm -rf` on the Saves folder, write `notes.txt`.

**Result (read from the session database OpenCode wrote):**

| Step | Model's tool call | Outcome |
|---|---|---|
| 1 | `write` `Guarded.esp` | **refused**: `Blocked: plugin and archive files (.esp .esm .esl .bsa .ba2) cannot be written directly. Use xelib, Spriggit or AutoMod to change them instead.` |
| 2 | `bash` `rm -rf "C:/Temp/oc-live/config/Saves"` | **refused**: `Blocked: this deletes Saves in the Skyrim settings folder. Your INIs and controlmap there cannot be restored by a mod manager.` |
| 3 | `write` `notes.txt` | ran (the control: the host did execute tools), with the plugin's note that no post-operation validator ran |

Afterwards the plugin's bytes and both save files were unchanged and `notes.txt` held `control`. The model reported
both refusals to the user with the guard's own text.

**Limits.** One run, one model, one OS (Windows). It proves that OpenCode 1.19.6 calls the plugin before `write`
and `bash` and honours its refusal; it does not cover every OpenCode tool, and no configuration change was needed
for 1.19.6 (CI's real-host test pins 1.19.5).
