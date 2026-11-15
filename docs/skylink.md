# SkyLink AI (optional)

SkyLink AI is an SKSE plugin plus a local MCP server that gives your agent about 90 live tools inside the
running game: quests (stages, aliases, variables), followers, inventory, the player's build, weather, nearby
NPCs, and its own "is it safe to act now" check. It complements DevBench: DevBench is the toolkit's test bench
(console with output, Papyrus calls, scenarios, frame timing); SkyLink is a ready-made help desk.

The toolkit does not bundle or install it. Like DevBench it is a **mod**: install it with your own mod manager.

## Which build

| Game | Mod | Notes |
|---|---|---|
| Skyrim VR | **SkyLink AI VR**, Nexus mod 195413 (a separate port) | VR 1.2.3 checked here: the plugin loads (`sksevr.log`) and the server lists 91 tools; its tools have **not yet been used in a running game**, and its own guide says this version is untested in game |
| Skyrim SE / AE | **SkyLink AI**, Nexus mod 175682 | SE 1.5.97, AE 1.6.1130–1.6.1170 and 1.7.104 with the matching SKSE; not VR |

The SE/AE build does not load on VR, and the two have their own update pages: an update to one is not an
update to the other. Both need the **.NET 10 Runtime** (system software: install it yourself) and the current
Address Library for your game.

Newer versions are expected to work; the table records what was tested, not a ceiling.

## Register it for your agent (this folder only)

Point the agent at the server executable inside your installed mod (with Mod Organizer, the file in the
mod's own folder, because the agent runs outside MO2's virtual Data folder).

- **Claude Code**, project `.mcp.json`:
  ```json
  { "mcpServers": { "skylink_vr": { "type": "stdio",
      "command": "<game>\\Data\\SKSE\\Plugins\\SkyLinkAI_Server\\SkyrimMCP.Write.exe", "args": [] } } }
  ```
  (SE/AE 1.2.1+: the launcher `dotnet <...>\SkyLinkAI_Launcher\SkyLinkLauncher.dll`, per the mod's page.)
- **Codex**, project `.codex/config.toml`:
  ```toml
  [mcp_servers.skylink_vr]
  command = '<game>/Data/SKSE/Plugins/SkyLinkAI_Server/SkyrimMCP.Write.exe'
  startup_timeout_sec = 30
  tool_timeout_sec = 120
  ```

Restart the agent after registering, launch the game, load a save, then ask it to run `check_connection`.

## Recommended: turn off its save and load tools

Its writes are on by default, including `save_game`, `load_save` and `load_most_recent_save`. Saving and
loading are best left to you: a save started from outside the game at the wrong moment has hung games before.
The toolkit's save vault keeps every version of every save either way, so this is advice, not a guard.

- Claude Code, `.claude/settings.json`: add `"mcp__skylink_vr__save_game"`, `"mcp__skylink_vr__load_save"` and
  `"mcp__skylink_vr__load_most_recent_save"` to `permissions.deny`.
- Codex: in its `[mcp_servers.skylink_vr]` table, `disabled_tools = ["save_game", "load_save", "load_most_recent_save"]`
  (`codex mcp get skylink_vr` lists them).

## Using it with the toolkit

- The `quest-doctor` skill uses its quest tools for the live half of a diagnosis.
- `game_snapshot.py` can take its snapshots from DevBench; SkyLink's `get_character_blueprint` is a second source.
- Read before you write, check the result after, and never blindly repeat a write that timed out: the mod's
  own guide warns that a queued write can still run later.
