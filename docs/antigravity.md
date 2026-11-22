# Google Antigravity

Status: **partially supported, unproven live.** The toolkit gives Antigravity what its documentation says it reads,
and a guard adapter that passes the toolkit's offline conformance checks. No live Antigravity session has run it
yet, so treat the guard as untested until the canaries below pass on your machine.

| Layer | What Antigravity gets | Source (antigravity.google/docs, read 2026-11-21) |
|---|---|---|
| Instructions | `AGENTS.md` (always on) and `.agents/rules/antigravity.md` | `/rules`: AGENTS.md, GEMINI.md and `.agents/rules/*.md` are combined |
| Skills | `.agents/skills/` (the same folders Codex reads) | `/skills` |
| Guard | `tools/adapters/antigravity/adapter.py` as a PreToolUse hook on `run_command`, `manage_task` (its `send_input`), `write_to_file`, `replace_file_content`, `multi_replace_file_content` | `/hooks` |
| MCP | your own `.agents/mcp_config.json` (below) | `/mcp` |

## Limits that matter

- **24,000 bytes per instruction file.** `AGENTS.md` is longer. Everything that protects your install (the safety
  rules, saves, file locks) is before the cut, with 1 KB to spare for the paths setup writes in; the working
  principles and the Codex section are at or after it.
  The rules file tells Antigravity to read the rest itself, and a repository test keeps the safety sections inside
  the first 24,000 bytes.
- **Hook failure behaviour is undocumented.** Antigravity's docs do not say what happens when a hook crashes,
  prints something unreadable or exceeds its timeout. The adapter answers `{"decision": "deny"}` for every failure
  it can see, and the hook timeout (40 s) is longer than the adapter's own 25 s deadline. Whether Antigravity
  honours a deny, and fails closed on a hung hook, is what the live canaries prove.
- **Unmapped tools are not guarded.** Only the five tools above are checked. `generate_image` writes an image to a
  place the docs do not name, and a subagent given write tools uses its own calls: neither is checked.

## Set up

1. Run `bash setup.sh --agent generic` (portable instructions and skills).
2. Dry-run, then register the guard:
   `python tools/adapters/antigravity/install.py` then `python tools/adapters/antigravity/install.py --apply`.
   It merges into an existing `.agents/hooks.json` and never overwrites one it cannot read.
3. Prove it (ADAPTING.md section 7): in a real Antigravity session in your Skyrim folder, ask it to write
   `Data/Canary.esp` with each write tool, and to run `echo x > Data/Canary.esp`. Each must be refused with the
   reason shown, and the file must not exist afterwards. Then run the broken-hook canary on a scratch copy.

Offline check any time: `python tools/adapter_conformance.py run --adapter-dir tools/adapters/antigravity
--profile tools/adapters/antigravity/profile.json --cases tools/adapters/antigravity/cases.json`.

## MCP (optional)

Antigravity reads `.agents/mcp_config.json` in the workspace, `{"mcpServers": {...}}`, with `command`/`args`/`env`
for a local server and `serverUrl` for an HTTP one. To give it House Carl, copy the server entry `setup` wrote for
Claude Code or Codex (`docs/housecarl.md`) into that file. DevBench needs no MCP entry: `tools/devbench-cli.sh`
talks to it over its local REST port.
