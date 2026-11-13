# The save vault

Since 4.1 the toolkit keeps **every settled version of every save** (the `.ess` and its `.skse` co-save, as one
pair) and of every settings INI, whatever changed it: you, the game, an agent, a script, a typo. Nothing asks first
and no rule reads a command to decide whether it touches a save. If a save is broken or lost, an earlier version is
in the vault.

This replaced the 4.0 save gate. That gate read every command's text to count the saves it changed, backed one or two
up and refused more until you typed an approval. Measured over 1,602 sessions it refused 191 commands and none of them
changed a real save; meanwhile a save changed by a script it could not read had no backup at all. The vault does not
depend on reading anything.

## What it does

- A watcher (`python tools/save_vault.py watch`) looks at every save folder the game reads every 15 seconds. A pair
  that has not changed for 20 seconds is copied in, hash-checked, and recorded. The game's own `.tmp` files are never
  copied.
- It also keeps every version of the INIs in the settings folder (`Documents/My Games/<game>/`), the one other thing
  with no copy anywhere.
- **Nothing is ever deleted from the vault.** A version that is already kept is not stored twice (files are stored by
  their hash), so a save you never change costs nothing more.
- A pair that disappears is recorded as `vanished`, with the last version kept.
- While the game or SteamVR runs, the vault never holds a live save open (it takes a hard-linked snapshot, so the
  game's save is never blocked) and it does at most one capture per look.

**Where it lives:** `%LOCALAPPDATA%\modding-agent-harness\save-vault\skyrim` (saves) and `...\skyrim-config` (INIs)
on Windows; `$XDG_DATA_HOME/modding-agent-harness/save-vault/` elsewhere. `SAVE_VAULT_DIR` moves it. It is outside
the game folder on purpose: a repair, an uninstall or a `git clean` there cannot reach it. It stops capturing when the
drive would drop under 2 GB free (`SAVE_VAULT_RESERVE_GB` changes that) and says so.

## Is it running?

Each Claude Code or Codex session starts with one line, `save vault: running (...)` or `save vault: NOT RUNNING`, and
starts the watcher when it is not running. Every agent's guarded tool call does the same check (one small file read),
so OpenCode and other hosts with no session start get it too. To check yourself:

```bash
python tools/save_vault.py status
```

Windows: `bash setup.sh --save-vault-task` (or answering yes at setup) adds a sign-in task, so the watcher also runs
while no agent session is open. It needs no administrator rights; `python tools/save_vault.py uninstall-task` removes
it. Linux: run `python tools/save_vault.py watch` from a `systemd --user` unit.

`SKYGUARD_SAVE_VAULT=off` stops the session line and the automatic start. `SKYGUARD_HOUSEKEEP_SWEEP=off` (no
background jobs at all) also stops the automatic start; the session line still says when the watcher is not
running.

## Finding and restoring a version

```bash
python tools/save_vault.py status Quicksave0           # every kept version of that save, newest last
python tools/save_vault.py status --vanished           # every pair that disappeared, with its last version
python tools/save_vault.py restore Quicksave0 --to C:/Temp/back            # dry run: says what it would write
python tools/save_vault.py restore Quicksave0 --at 2026-11-10T21:00 --to C:/Temp/back --apply
```

- `--at TIME` picks the last version at or before that time; `--id ID` picks one exactly (from `status`).
- Where it goes: `--to DIR` (a new folder, never over a file there), `--as NEWNAME` (beside the original under another
  name), or `--replace` (over the current pair, which is kept as a version first). With none of these it puts a
  vanished pair back where it was.
- **Nothing is written without `--apply`.** A restore is all-or-nothing: every file is hash-checked before any is
  written. A restore into a save folder is refused while the game (or SteamVR) runs; run it again once it is
  closed. `--replace` removes the set-aside current pair only when its bytes are the kept version's; otherwise
  it stays beside the save as `.vault-old-*` and the event log names it.
- `--config` acts on the INI vault: `python tools/save_vault.py --config restore SkyrimPrefs --to C:/Temp/ini`.

Backups the 4.0 save gate took stay where they were and stay restorable:
`tools/skyguard.sh snapshot list --kind save`, `python tools/agent-harness.py save-list` / `save-restore`.
`python tools/save_vault.py import-legacy` copies a 3.x store (`.claude/backups/save-backups`) into the vault; it
never changes the old copies.

## Limits (measured or by design)

- **A version that exists for less than about 35 seconds** (20 s settle + 15 s look) may never be captured: two
  quicksaves inside that window keep the second.
- It protects only while the watcher runs. The session line and the per-call check say when it does not.
- It is on the same disk as your saves. A second copy elsewhere (another drive, a NAS) is up to you; the vault's
  `store/blobs` folder holds files named by their own SHA-256, so any copy tool that adds without deleting works.
- Folders the game does not read are not watched unless they hold a `.protect-saves` file, or you name them with
  `--folder` or `SKYRIM_SAVE_DIRS`.

## What the guard still refuses about saves

Deleting or moving a **whole** `Saves` folder, the settings folder that holds it, or everything in `Saves` at once
(`rm -rf Saves/*`) is still refused (S-DEL-CONFIG, S-MOVE-ROOT). Writing into the vault or the old backup stores is
refused (S-SAVE-STORE). The optional OS save locks (`tools/skyguard.sh save-lock on`) are unchanged: a locked save
cannot be deleted or changed by any non-administrator process. Everything else about a save (reading, copying,
editing, ReSaver `--apply`, a DevBench save) runs without a rule.
