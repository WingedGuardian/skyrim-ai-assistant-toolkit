# Native observation contract, schema 1

Every response has `schemaVersion`, `ok`, `source: ToolkitProbe`, `runtime`
(`SE`, `AE`, `VR`), `sessionId`, `sessionGeneration` and `generation` (same integer),
`observedAt` (Unix seconds), `coverage`, `truncated`, `timings`, and `data` or
`error`. `timings.budgetMs` is 5; successful observations also report
`mainThreadMs`. An error is never an empty successful result. C++ exceptions
become errors. Both event task and public handler boundaries catch exceptions.
Final encoding uses explicit UTF-8 replacement for malformed engine names and
paths (`encoding: utf8-replacement`). Serialization failure uses a static ASCII
error envelope (runtime/session/timestamp unknown); the host writer is called
once and is never retried if it throws. Access violations are not recoverable
C++ exceptions.

Registration is attempted once at SKSE kDataLoaded, after all plugin kPostLoad
callbacks have completed. DevBench 1.35 initializes its HostApi/listener during
kPostLoad, so requesting the interface in that same stage is order-dependent.
Absent or below-floor hosts remain an optional no-tool condition.

The session identifier changes on process restart. Generation changes on
pre-load/new-game messages. A queued request checks generation, its four-second
steady-clock deadline and cancellation before dereferencing objects and before
publishing its result. The task captures only owned arguments/request state;
it never retains the DevBench sink or writer. Exactly one listener-thread write
happens before the handler returns. No per-frame observer is installed.

A cooperative five-millisecond/5000-node traversal budget checks between work
items. Native engine calls, lock waits, one item, sorting and JSON construction
are not preemptible; the budget is **not** a proven maximum frame time. All engine
object dereferences happen in queued main-thread tasks. Event callbacks retain
an owned NiPointer without dereferencing the target, filter watched object
addresses under a mutex, and copy minimal owned payloads into a global queue of
at most 2,000 events, including in-flight events. Overflow drops the new event
and increments each enqueue-time recipient's `eventQueueDropped`. No event
source is silently filtered. One outstanding event-triggered dispatcher drains
at most 50 records under one cooperative five-millisecond budget, including
source lookup, row publication and visual work. It never schedules itself:
backlog resumes only on another incoming event or a watch read/stop. A quiet
backlog may wait for the next normal five-second CLI poll plus bounded draining.
Explicit reads/stops share that request's existing budget. Per-drain bounds do
not bound aggregate work per frame under arbitrary external stimuli. The
[task-pump source audit](TASK-PUMP-AUDIT.md) establishes why AddTask is not a
next-frame continuation. A queued event owns immutable capture IDs and
generations selected at enqueue.
Supersession cannot redirect it into a replacement capture: already accepted
events may finish into the original terminal capture. Its visual observation
is skipped after stop/expiry. Boundary failures appear as `eventBoundaryFailures`
and per-capture `eventFailures` where the recipient still exists.
Per-capture pending admission increments under the capture lock and settles
exactly once on delivery, abort or destruction of an unrun queued task. Terminal
captures with pending callbacks cannot be evicted from the retention map.
Generation invalidation actively discards/settles queued work and releases its
owned targets even without another event/read. Dispatcher disposal or failed
submission visibly fails/settles its queued admissions once; a stale delegate
cannot affect a replacement ticket. Reads that empty the queue retain the
outstanding delegate ticket until its own run/disposal. Submission never occurs
while captureMutex is held. Neither a worker nor an engine observer is added.

## Arguments and data

- `probe.look`: `from=hmd|right|left|crosshair` (VR defaults hmd; flat defaults
  crosshair), `max=2000` (0 < max <= 10000), `limit=20` (1..100).
  `data={refs,no3D,ray,collision,resultLimitReached,rankingComplete}`. `ray` gives
  origin/direction arrays. `collision={status,method,hit,distance}` identifies
  the first collision result before bounds candidates. The Havok implementation
  uses `TES::Pick`, `bhkWorld::GetWorldScale` and `FindCollidableRef`. Player hits
  are skipped; the ray is not retried past a player hit. Crosshair uses the
  camera-root center-forward ray rather than an off-center screen/UI cursor.
  Flat runtimes reject hmd/hand poses; all runtimes reject unknown pose names.
- `probe.near`: `radius=500` (0 < radius <= 10000), `at=player|[x,y,z]`
  (finite coordinates, magnitude <= 100000000 each),
  `types` (comma-separated exact base form-type names), `limit=20`.
  Empty types means no filter. Same data shape as look with null ray and
  collision not checked.
- `probe.ref`: `formId` hex string; returns `data.ref`.
- `probe.effects`: `actor` hex or player; returns
  `data={effects,visuals,effectsCoverage,visualsCoverage}`. Missing MagicTarget or
  ProcessLists is explicitly unavailable; an unavailable visual observation is
  never a complete-empty snapshot.
- `probe.watch`: actor and integer seconds 1..600 (default 180) start a capture.
  `{read:true,captureId,after:sequence}` reads; `{stop:true,captureId}` stops.
  `data={captureId,actor,events,nextSequence,dropped,cursorLost,active,status,
  visualSnapshot,visualSnapshotComplete,visualStatus,eventQueueDropped,
  eventFailures,visualEventsDropped,pendingEvents,latestSequence,hasMore,
  maxDeliveryDelayMs,queueHighWaterMark,maxDrainMs,drainBudgetStops,visualSampling}`. Integer fields
  seconds, limit and after require an integer JSON value (no floats, negatives
  or narrowing/wrapping conversions); after is capped at 9007199254740991.
  `nextSequence` is the last sequence **returned in this page** (or the input
  cursor for an empty page). Read at most 50 events per call, then immediately
  drain `hasMore` using nextSequence, including on terminal captures.
  Reads first shallow-page already-published events before pending delivery can
  evict ring rows; final `hasMore`/`latestSequence` include newly delivered rows.
  `maxDeliveryDelayMs` measures the maximum callback-to-publication delay among
  delivered direct events (not the age of still-pending events); original event
  timestamps remain callback timestamps. `queueHighWaterMark` is the maximum
  GLOBAL pending occupancy experienced while this capture had admitted work,
  not solely this actor's events. `maxDrainMs` and cumulative `drainBudgetStops`
  report drains that affected this capture, including a zero-progress budget
  stop on its front queued event. Cooperative work can exceed five milliseconds
  within one unpreemptible engine call or item. `visualSampling` is explicitly
  `per-actor-capture-drain`; start/read snapshots also remain available. A capture
  stopped by the request becomes terminal before any queued delivery/visual work.
  `pendingEvents` counts admitted callbacks for this capture that have not yet
  delivered or aborted. A terminal response with pendingEvents > 0 is not final:
  continue bounded paced reads (CLI: up to five seconds), then report incomplete
  if pending callbacks remain. The unchanged cursor is valid during this wait.
  `latestSequence` is the capture watermark and may exceed nextSequence. Pages
  are copied as shallow owned event pointers on main; event expansion and JSON
  encoding happen on the listener. An ahead-of-watermark cursor is an error.
  Capture status is active, expired,
  stopped, superseded or invalidated. A restart for an actor supersedes its prior
  capture. A queued event accepted before that restart remains with its original
  capture, never the replacement. Four actors may be active, 2000 events per
  actor are retained, and the
  oldest retained event detects cursor loss. At most 32 captures are retained;
  discarded terminal captures subsequently return an unknown-capture error.

All identity objects use eight-digit hexadecimal `formId`, name, editor ID,
formType and `runtimeSourceFiles`. A reference includes its separate `base`
identity, `baseFormId`, mesh path when exposed by TESModel RTTI, position,
loaded3D, disabled, cell, bounds and optional shapes. The runtime source-file
array is not asserted to be xEdit's full override chain; CLI checks preserve
any disagreement. Dynamic records may have an empty array.

Bounds are loaded world spheres: broad phase uses root bounds, then candidate
shape spheres determine entry/surface distance and selected shape. Reference
origins do not filter loaded geometry. Candidate lists sort by nearest shape
entry after the collision hit. If traversal stops, ranking applies **only to
observed candidates**, never globally nearest or complete. No-3D references
have positions and no geometry claim. Their separate list is bounded and does
not establish absence. Terrain without a reference is not fabricated as one.

Shape textures distinguish `source=runtime lighting texture set` (requested
numeric slots 0..8), `source=runtime effect material path` (assigned effect
source/greyscale paths), and `source=loaded source texture` (independently
observed loaded object names). Nonempty effect paths survive null loaded
objects and disagreements with loaded names; both observations retain their
slot and source label. Null loaded objects do not establish asset absence.
Lighting diffuse/normal, effect source/greyscale, and reference
effect fill/holes/palette/particle paths are observed. Lighting environment, glow,
height/parallax, face, eye, multilayer, landscape
and LOD landscape subclass assignments are also included where the audited
CommonLib feature discriminator matches. Water/base materials remain incomplete.
These
are engine-assigned texture names, **not proof of GPU draw-time bindings**.
Asset existence/provenance remains the offline CLI/House Carl layer.

Active-effect rows expose their own MGEF, source magic item, caster, elapsed
and duration. VR uses AsMagicTarget plus VisitActiveEffects; flat runtimes use
GetActiveEffectList. Visuals use ProcessLists' lock-protected model/shader
reference-effect list and target handle, with direct art/shader form identity.
Their initiating spell/source attribution is unknown.

Watch records applied MGEFs, hits and spells **cast by** the watched actor.
A cast event does not establish a target and is never labelled a cast at it.
Each direct event copies source identity (or null with sourceStatus unavailable),
sourceFormId, sourceKind (spell, magicEffect or hitSource), and event semantics.
An application source is its MGEF, never a guessed initiating spell.
Visual snapshots run at start, reads and coalesced event drains, capped at 100
matching visuals. A drain samples at most once per affected actor/capture;
callback timestamps on direct events do not label those coalesced observations
as per-event samples. Reads avoid a second snapshot for a capture already
considered by that drain. Stopped/expired captures cannot take event-drain
visual samples, although their already accepted direct events can finish. A partial/unavailable initial observation establishes no baseline: the
first complete observation establishes it without invented attachment events.
Later incomplete/unavailable observations preserve the last complete baseline
and cannot invent removals. Complete snapshots produce indexed attached/removed
differences by engine effect ID. Budget exhaustion while writing differences
counts unrecorded changes in visualEventsDropped. They can miss brief
transitions and cannot distinguish effect-ID reuse between samples. Differences
have unknown attribution: temporal proximity is not causation. Overflow and
partial snapshot coverage are explicit; there is no continuous polling loop.

## Evidence boundary

Windows compilation checks the exact accessed CommonLib members across the
multi-runtime layout configuration. Portable tests cover ray/sphere distance,
distant-origin bounds, per-shape ranking choice, strict hex and JSON validation,
bounded ring paging/cursor loss, enqueue/restart/deliver ownership, partial
visual baseline handling, encoding/fallback once-write and generation/deadline
predicates. Neither proves live pose orientation, collision locking/units, real event dispatch,
material content, loaded-cell coverage or frame time. Production queue helpers
also exercise bursts beyond four, the 2,000-event bound including in-flight work,
partial/zero budgets, order/timestamps, actor coalescing, supersession/stop/load,
failed/ambiguous submissions and unrun delegate disposal, once-only settlement,
visual/row exceptions, measured budget telemetry and page-before-drain retention.
These deterministic checks do not establish zero loss under real traffic. A1-A6 and real headset
acceptance remain pending protected runtime tests after review and launch consent.
