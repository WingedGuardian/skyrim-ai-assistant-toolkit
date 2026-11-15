---
name: quest-doctor
description: Work out why a quest is stuck, will not start, will not advance or shows no objective -- by comparing what the plugins define (stages, aliases, conditions, who overrides the quest) with what the running game reports (stage, running, completed, objectives). Use when the user says a quest is broken, stuck, missing from the journal, or "won't progress". Diagnoses first; suggests fixes, never applies one unasked.
---

# Quest doctor — why is this quest stuck?

The answer is almost always one of four things, and each needs different evidence:

| Symptom | Usual cause | Evidence that settles it |
|---|---|---|
| Never starts | its start condition or story-manager event never fired; a required alias did not fill | the quest record's conditions and aliases; live `running 0` |
| Stuck on a stage | the next stage's trigger (a dialogue line, a script, an alias death) is unreachable or was overridden | the stage list; which plugins override the quest or the trigger |
| Missing from the journal | objectives not shown, or the journal stopped listing them | live objective states vs stages done |
| "Completed" but not done | a mod set the stage without the script, or another mod's patch broke the stage fragment | completed flag vs stage list; the override chain |

## 1. Identify the quest

Get its FormID from the user, the console (`help "<name>" 4` lists QUST matches), or House Carl. Use the
plugin-qualified identity (`XXXXXX:Plugin.esp`) from here on.

## 2. What the plugins define (offline, no game needed)

- **Who changes it:** `bash tools/skyrim-winner.sh chain <formid>` — every plugin that overrides the quest, in
  order. A quest overridden by a mod that has nothing to do with it is the first suspect.
- **The record itself:** House Carl (skill `toolkit-housecarl`), `housecarl_records` on the QUST record with
  `project.form=tree` to compare the winning override with the original: stages (and which have scripts),
  objectives, aliases (fill type, conditions, "optional"/"allow reserved"), start conditions, and the
  "start game enabled" / "run once" flags. Consult the tool schema before building the request.
- **The trigger of the stuck stage:** find what sets the next stage (a topic info, a script on an alias, a
  scene) and run `skyrim-winner chain` on that too.

## 3. What the running game says (read-only)

- `python tools/resaverhelper.py quest <formid>` — editor id, current stage, running, completed, and each
  objective's state (needs ReSaverHelper; skill notes in `tools/resaverhelper.py --help`).
- Without ReSaverHelper: `bash tools/devbench-cli.sh call Quest GetCurrentStageID '[]' '{"form":"<formid>"}'`,
  and `IsRunning`, `IsCompleted`, `IsStageDone` (skill `devbench`). These are reads, but they run on the main
  thread: check `bash tools/devbench-cli.sh alive` first.
- For alias problems: `bash tools/devbench-cli.sh inspect quests` and the quest's alias references.

## 4. Diagnose, then propose

Say which row of the table above it is, with the evidence for it. Then propose the smallest fix that keeps
the game's own logic:

1. A stage whose trigger is unreachable: `setstage` to the stage the trigger would have set, so its fragment
   runs (`python tools/resaverhelper.py setstage <formid> <stage> --apply`, or the console). Never skip past
   a stage whose fragment hands out items, starts another quest or moves an NPC without saying what is lost.
2. Objectives missing from the journal: `python tools/resaverhelper.py refresh --apply` (journal refresh,
   missing objectives only).
3. A broken override: a patch in the user's conflict-resolution plugin (skill `toolkit-housecarl`), not an
   edit to the mod.
4. A quest that refuses its completion stage: `complete` (no stage script runs) only as a last resort, with
   the user told exactly what the skipped scripts would have done.

Every one of these changes the save the user is playing: ask first, name the save, and remember that the save
vault keeps the previous version. Never use the helper's SAVEGAME/RESCUE, and never load or save from here.
