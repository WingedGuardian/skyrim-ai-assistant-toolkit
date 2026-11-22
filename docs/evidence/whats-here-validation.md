# What's here: validation evidence (2026-11-14 to 2026-11-16)

What the live tests of `tools/whats-here.py` and ToolkitProbe established, and what they did not. All runs were on
Skyrim VR (SKSE VR, DevBench 1.35/1.36), started headless on a private desktop with a test save, and the
install was restored and checked byte-for-byte after each one. **SE and AE have no live runs yet**: ToolkitProbe builds
for all three runtimes from one source, but a successful build is not evidence that it works there.

## Acceptance criteria and results

| | Criterion | Result | Limit |
|---|---|---|---|
| A1 | `look` names a known object placed in front of the player first | **Pass.** The first Havok collision hit was the exact marker reference placed for the test. | Headless: a fixed engine pose, not a real headset or hand pointing. |
| A2 | A deliberately missing texture is reported missing; one that a BSA provides is reported present | **Pass.** The fixture's missing `.dds` was reported missing on the right shape; a vanilla texture packed in a BSA was reported present. | Material paths are what the engine assigned, not proof of what the GPU sampled. |
| A3 | A mesh whose origin is far away but whose geometry reaches the player appears in `near` | **Pass.** Root distance 596.97 (outside the 500 radius); the nearby shape was found at 171.75. | Bounds are world spheres per loaded shape, not exact geometry. |
| A4 | A spell that fires every 30 s shows up in `watch` with its source each time | **Pass.** 180 s capture; three casts at 2.1 / 32.2 / 62.1 s each produced exactly one matching magic-effect application (1,804 events in all). Zero dropped, failed or pending events; max delivery delay 10.4 ms, max native drain 0.24 ms. | The engine sent no separate cast event for a script `Spell.Cast`. What attached a visual is not visible natively (see visual attribution). |
| A5 | The `--check` override chain matches `skyrim-winner chain` for 20 references | **Partial.** 20 references and their base forms (40 checks): the winning plugin agreed 40/40; full chains agreed 38/40. The two differences were an extra `Update.esm` row offline. Both results are kept, never merged. | Checked offline after the game closed (live `--check` ran out of memory beside the game, which led to the 12-form cap in 4.2). Not atomic with the runtime read. |
| A6 | No call takes over 50 ms of main-thread time at radius 500 in a city | **Pass in that scene.** 40 calls in Whiterun (10 each of near/look/effects/ref); max 4.24 ms. | 20 of the 40 hit the result limit. One scene measured, not a universal bound or a frame-time measurement. |

## Real-symptom runs (recurring blood on the player)

- A 360 s ordinary combat capture with a full 720-item inventory snapshot ran with zero loss. The blood did not appear
  during it, so it proved the collector, not the cause.
- A later 330 s watch during a controlled fight (1,257 events, none dropped) caught two blood effect-shader
  attachments, each 0.1-0.5 s after the Ordinator perk effect "Unleash the Beast" proc'd. The records explain the
  cadence: while in combat and dual-wielding, a script re-casts the proc every 20-40 s, and the proc carries a
  blood-splatter shader. This led to the timing-based **visual attribution** (`inferred:timing`): replaying that
  capture through the CLI names the proc (ratio 1.0 over 2) over an unrelated effect applied 562 times (0.009). A
  repeat run (420 s) did not trigger the perk, so the join has been exercised on one live capture only, and the run
  was headless, so the overlay itself was not seen.

## Defects the runs found (all fixed before 4.2)

- Watch admission could drop events under load: replaced with bounded batching (2,000 pending, 50 per drain, 5 ms).
- Plugin-qualified identity for `--check` was taken from a form's first override: now read from the game's own
  full/light index map.
- Helper processes opened visible console windows during a run: every helper now starts hidden, with tests that spawn
  a real child and check it has no console.

## Still open

- Real headset and hand pointing (headless poses only so far).
- SE and AE live runs.
- A second live capture of a visual whose cause is known, to exercise the timing attribution again.
