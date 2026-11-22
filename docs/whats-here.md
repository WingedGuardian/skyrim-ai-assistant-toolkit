# What's here

`tools/whats-here.py` combines live DevBench observations with optional ToolkitProbe observations and offline asset inspection. It addresses object identity, nearby visual defects and recurring effects without requiring desktop-console interaction.

**Status:** live-tested on Skyrim VR (headless; see [validation evidence](evidence/whats-here-validation.md)). SE and AE build from the same source but have no live runs yet, so treat them as experimental. Real headset and hand pointing are not yet tested.

## Commands

Run `python tools/whats-here.py --help` for the installed options. Commands are `look`, `near`, `ref`, `effects`, and `watch`; `--json` retains structured observations and coverage. `--game-dir` selects the installation and its DevBench runtime endpoint. Connections are loopback-only; no command starts the game.

Exit codes: `0` complete; `3` observed but incomplete (a partial, superseded or interrupted watch, or a `--check` that did not finish every form); `2` failed. `--check` checks at most 12 forms per call, because each one loads the whole load order in xEdit; the rest are listed as not checked.

On Windows, the CLI starts its House Carl and winner-check helper processes with `CREATE_NO_WINDOW`. The headless launcher's process-list and ancestry helpers use the same existing subprocess flag in either desktop mode. This suppresses helper console creation; it does not qualify windows or child launches inside optional external tools. Audit those tools before a live test. Focused regression checks mock the production calls before using their validated kwargs for real console-child probes; no unhidden negative probe is needed.

Typical questions:

| Question | Observation |
|---|---|
| Which object am I pointing at? | `look`, then `ref` if more detail is needed |
| Which nearby mesh owns this visual defect? | `look --textures`, then `near --textures` |
| What keeps affecting my character? | `effects`, then `watch --seconds 180` |

The CLI uses native `inspect refs`, `inspect effects` and `forms describe` as primary observations. ToolkitProbe supplements them with geometry, material and capture information. Without the DLL, native diagnostics remain available with explicit limitations. Native nearby-reference enumeration uses origins and does not establish that a reference's geometry is within the requested bounds distance. Console selection is not an HMD/hand ray.

## Inventory and optional SkyLink context

Use `effects --inventory` for one current native inventory observation or `watch --seconds 360 --inventory` for before/after inventory around a recurring-effect capture. Add `--game-dir` and `--json` as needed. `--inventory-limit` defaults to 500, accepts 1..1000 and requires `--inventory`; inventory flags are rejected for look/near/ref before connecting. DevBench already implements inventory inspection, so this adds no bridge or native DLL dependency.

The additive `inventoryContext` reports completeness and timestamped native snapshots, retaining owner identity, counts, returned items, truncation and errors. Watch inventory calls stay outside capture/poll/drain/owned cleanup; a load, instance change, invalidation, failure or unavailable capture can leave the final snapshot uncollected. Partial inventory context does not erase valid effects/events or change their existing success/exit semantics. Without the flag there are no additional inventory requests. A response limit bounds returned rows, not a measured main-thread execution budget; no per-frame inventory scanner is introduced.

The diagnostic skill can supplement the CLI through an already registered SkyLink connection. It checks connection/player context, reads equipped slots/hands and inventory pages (200 items/page, at most 1000 items per snapshot), and uses returned enchantments plus at most eight deduplicated targeted enchantment reads across the investigation. It stops enrichment at the first unavailable tool/refusal/timeout. It never starts another pipe client, drains `poll_events`, repeatedly collects blueprints or invokes save/Papyrus/gameplay operations. The CLI JSON remains native-only; SkyLink data appears as separately labelled agent context. Non-player diagnostics must not inherit the player's SkyLink equipment.

Record source and observation time for each provider. Unverified cross-bridge identity, truncated lists and unexamined candidates remain explicit. Runtime base FormIDs do not distinguish differently enchanted copies of one item. Matching an enchantment/effect is candidate evidence, not script-caller proof.

Effects baselines enumerate engine ActiveEffect entries; this version does not export inactive flags or per-effect condition state. Several listed blood-tier records can belong to one conditional ability, so membership alone does not establish which tier is enabled or rendered. Application/cast events and observed reference visuals are separate evidence.

### Recurring blood on the body, hands or equipment

1. Collect native effects/inventory and optional SkyLink gear context. Include unnamed carried entries and controller items as well as worn armor.
2. During an agreed 360-second ordinary-play observation, mark reported blood onsets and compare active effects, temporary shader/art attachments and supported player-reference materials. Do not assume the player's returned 3D covers every first-person hand/body overlay. Collect gear context again after a valid capture; calls are separate, not atomic.
3. Resolve observed MGEF/EFSH/ARTO/spell/enchantment/item records using runtime identity and supported offline records/asset tools. Inspect candidate VMAD bindings, quest aliases/properties and winning deployed script/bytecode. Use House Carl's supported gateway and existing script tools; runtime IDs need verified plugin-qualified mapping.
4. Separate the asset's owner from the code that invoked it. If the visual is not observed or the caller remains unknown, retain the investigation as open and name the missing observation before proposing targeted instrumentation. Do not infer a culprit by clearing effects, disabling mods or running arbitrary functions.

This real-symptom capture is separate from the 180-second synthetic acceptance watch. Neither this documentation nor implementation approval authorises another game launch or automatic load/save.

### SkyLink versions

SkyLink is optional. The VR build is [Nexus 195413](https://www.nexusmods.com/skyrimspecialedition/mods/195413) and the SE/AE build is Nexus 175682; they are versioned separately. The skill records the version that answered as provenance and accepts newer compatible releases.

SkyLink for SE/AE 1.5.0 is more than a read bridge: it can install plugins into MO2 and activate them (`install_plugin`), sort the load order with LOOT (`sort_load_order`) and clean plugins with SSEEdit (`clean_plugins`). The toolkit's guard refuses those three on any MCP server name (any case on Codex and OpenCode; snake, Pascal, camel or kebab case on Claude Code) (S-MCP-WRITE), on Claude Code, Codex and OpenCode, and gives `execute_console_command` and `call_papyrus_function` the same crash-command check as DevBench's console. Not refused: its reads (What's here's use of them is unchanged), its builders (`create_plugin`, `compile_script`, `package_override_mod` write only into SkyLink's own Build folder) and its save tools (the save vault keeps every version, as for DevBench). The names were checked against the 1.5.0 server's own tool list; not tested in a live SE/AE game. Antigravity does not route MCP calls through the guard.

## Interpretation

- A collision hit and a ray/bounds candidate are different results. Incomplete traversal cannot establish a globally nearest result; ranking applies to checked candidates.
- Loaded material texture assignments, loaded texture-object names and offline NIF texture paths are separate evidence. They do not prove what a specific rendered frame sampled. Empty slots do not establish missing files. Effect source/greyscale assignments remain visible even when the loaded object is null or has a different name; asset checks include each observed path.
- Offline asset availability is not the runtime provider winner. House Carl coverage warnings remain in the answer; absence is conclusive only when the provider checks establish it. Text output retains offline NIF failures and unchecked meshes even if the subsequent asset check succeeds.
- A spell cast by an actor is not necessarily a spell cast at that actor. Direct source information and correlation remain separate. Event-driven visual snapshots may miss attachments or removals between observations.
- Reference and base-form provenance are separate. `--check` preserves the offline and runtime results when they disagree. Native `forms describe` metadata is not an origin or override-chain guarantee. `--check` resolves the runtime full/light plugin namespace through the observed native mod-index map and checks reference and base separately. It preserves reported metadata and raw offline results; missing mappings or failed checks set `validationIncomplete`. File load order may differ from runtime, and injected records may still lack a matching file record.
- Load/new-game transitions invalidate captures. Expiry, supersession, cursor loss and dropped events must be reported, never presented as an uninterrupted complete watch.

The bounded-batching follow-up keeps at most 2,000 admitted pending events, including in-flight work. One event-triggered native drain handles at most 50 records under a cooperative 5 ms budget; remaining work waits for a later event or explicit watch read/stop. It never schedules its own successor. Visual observations are coalesced per actor/capture drain and can miss transitions between drains. Callback time and delivery time are different; no queue size guarantees zero loss.

The CLI assists active backlog in cycles of at most 41 responses and five seconds, with one fixed deadline per cycle. Pending-only reads are paced at 50 ms; published pages can drain immediately. An unchanged consumed cursor is retried only when pending work decreased or a valid publication watermark advanced since the previous response; these retries remain paced, and publication is not counted as delivery. An exhausted active cycle reports remaining backlog and returns to the normal five-second poll cadence while retaining the requested capture duration. It does not terminate the capture merely because background traffic continues. At termination, the separate five-second settlement window allows at most 41 published-pagination responses and 101 pending-assistance responses, followed by at most two seconds of owned cleanup. The first terminal reply counts as pending assistance only if it has pending work without rows or more published pages; otherwise it counts as an initial published page. Follow-ups inherit the intent that requested them, even if a pending-assistance reply incidentally includes rows. Neither category budget resets on progress, and the two disjoint allowances permit at most 142 terminal responses under the same wall deadline. These are explicit cutoffs: 2,000 unread ring rows plus 2,000 pending admissions, expanded visual rows or ongoing traffic can require more work than one cycle permits. Unsettled terminal work is reported incomplete.

Watch reports maximum delivery delay, maximum native drain time and cumulative native budget stops. Queue high-water mark is the maximum **global pending occupancy experienced**, not solely this actor's events. `visualSampling=per-actor-capture-drain` names the coalesced sampling scope. Retain active-cycle cutoff telemetry, queue omissions, pending counts and partial/error coverage alongside these timings. Batching and CLI drain changes require another approved runtime qualification; offline checks do not establish the resulting live loss or frame-time limits.

### Provenance under memory pressure

`--check` invokes the existing xEdit winner helper and can need substantial memory alongside a game. Retain native reference/base observations plus the actual DevBench `inspect kind=mods` compile-index map and file-order authority/hash. For an owned test, restore/release immediately after exit, then use existing offline chain tools on the retained qualified identities. During ordinary play, defer that work until the user closes the game. On exit137 inspect the machine guard and partial outputs; do not retry before memory recovers.

This is an agent-level two-stage workflow; the live CLI still requires DevBench. Source-file lists, namespace mappings, offline full chains and the reported winning plugin remain separate evidence. A matching winner does not erase a full-chain discrepancy or identify the script that invoked an effect.

### What Am I Looking At (SE/AE)

On SE and AE, the separate mod [What Am I Looking At](https://www.nexusmods.com/skyrimspecialedition/mods/199906) saves the object you click in the console to a `last-pick.json`. When one exists under `Data/SKSE/Plugins`, `look` attaches it as `externalPick` with its path and age. Its format is that mod's own and is not checked here, so the content is passed on as written and never merged into `look`'s fields.

## Optional native extension

ToolkitProbe requires SKSE for the runtime, its appropriate Address Library and DevBench 1.35 or newer. Newer compatible DevBench releases are accepted. Build dependencies are separate from these runtime requirements. See [native source and build instructions](../tools/toolkit-probe/README.md).

Install the optional `ToolkitProbe.zip` through the mod manager, or the existing `tools/mod-install.py` dry-run/apply workflow for an authorized manual install. This CLI does not install it, change load order or mutate gameplay. Uninstall through the same manager/installation receipt while the game is closed.

The Windows build workflow produces an optional archive with a nested `source.zip` containing exact committed source and licenses, plus a source/DLL receipt. Only `Data/SKSE/Plugins/ToolkitProbe.dll` maps to installable content; keep `source.zip` as source evidence rather than installing it as another mod. `scripts/build_toolkit_probe.py` binds source before compilation and refuses drift; `scripts/package_toolkit_probe.py` verifies the receipt and packages committed source blobs. Provenance describes build inputs; it does not assert live acceptance. Each toolkit release attaches the `ToolkitProbe.zip` its release workflow built from the tagged commit, with a build attestation.

## Upstream and qualification

[DevBench issue 185](https://github.com/alandtse/devbench/issues/185) requests only the observation gaps after auditing its existing native tools. Upstream acceptance does not block the independent extension; accepted native tools can replace the extension backend later.

Live results for the six acceptance criteria, the real recurring-blood capture and what remains open are in [validation evidence](evidence/whats-here-validation.md).

**Visual attribution (2026-11-16).** The native probe cannot see what attached a shader or art object, so `visualAttached` arrives as `unknown`. After capture, `watch` joins each visual to the magic effects applied within 1 s before it. It ranks them by how often each one's applications were followed by that visual, and sets `attribution: inferred:timing` plus `inferredSource` when the best ratio is at least 0.5. Removals inherit the attachment's inference. Up to 5 candidates are listed in `visual.candidateSources`, and the per-visual summary is in `visualAttribution`. This is timing evidence, not an engine link. On the recorded blood capture it names `ORD_One_UnleashTheBeast_Effect_Proc` (ratio 1.0 over 2) over a dummy applied 562 times (0.009).
