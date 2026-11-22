# Watch dispatcher scheduling audit

Audit date: 2026-11-14. Product source audited: `f1778e341f018a6e5e8de5dd93ccf81cc5e8e4f8`.

## Result

SKSE `AddTask` does not supply a next-frame or next-pump continuation boundary.
The official source matching the installed VR runtime drains its live queue until
empty, including tasks added by a running task. Therefore a dispatcher that
processes 50 events or five milliseconds and then calls `AddTask` again can run
repeatedly in the same pump. A per-drain budget does not bound that aggregate
work. Do not implement recursive scheduling on the assumption that it yields.

This is the specific failed scheduling contract requested by the frozen watch
follow-up spec. No queue implementation, engine hook, persistent observer,
worker, or new launch was introduced during this audit. At this audit checkpoint the four-task admission limit and its measured
omissions remained unchanged. The reviewed amendment below was implemented
after the scheduling gap was reported.

## Source provenance

The [official SKSE download page](https://skse.silverlock.org/) identifies VR
2.0.12 for game 1.4.15. The audited source and binary are in its linked
[official VR archive](https://skse.silverlock.org/beta/sksevr_2_00_12.7z).
These hashes record the actual inputs; they are not tool version ceilings.

| Input | SHA256 |
| --- | --- |
| `sksevr_2_00_12.7z` (1,010,226 bytes) | `F03DF5D8663F2C9A781F830FB0809C63A9A0E3B626D6D1A96E38493F81A3C9AD` |
| Archive `sksevr_1_4_15.dll` | `633BE638897343625D755A6207C9C805AA1B9E82245FBEF1B3B154C91A973463` |
| Installed `sksevr_1_4_15.dll` (version 0.2.0.12) | `633BE638897343625D755A6207C9C805AA1B9E82245FBEF1B3B154C91A973463` |
| Archive `src/sksevr/skse64/Hooks_Threads.cpp` | `7C3706BD85841E6D2F7B03E00E1971A8EDB32ED78938C0EC6CF1E2A5D47F0AA3` |
| Archive `src/sksevr/skse64/Hooks_UI.cpp` | `AF9225BCA190AA9458093D3A0F75F37611DA8DF6CB6809FE08E5A4CDD5A1CD4A` |
| Archive `src/sksevr/skse64/PluginAPI.h` | `D7D47EC8E6643CFF46E1D31A2988CB4A46F9A71D81F68296AEF9D6A620A80A87` |
| Consumed CommonLib `include/SKSE/Interfaces.h` | `453DA77C755EE6855F653BA0F8EFF365CD8C4037E789AB62339416C0945497AD` |

Consumed CommonLib source is commit
`f82241e5daabfaadace7706bea14a202a7916e9c`. Its `Interfaces.h:393-396`
forwards task submissions directly to the SKSE proxy; its callable overload
allocates a delegate. It adds no deferred continuation semantics.

## Scheduling evidence

Archive paths below are relative to `sksevr_2_00_12/src/sksevr/`.

- `skse64/PluginManager.cpp:64` binds the task-interface function to
  `TaskInterface::AddTask`.
- `skse64/Hooks_Threads.cpp:13-27` implements `BSTaskPool::ProcessTasks`.
  It enters the queue critical section, repeatedly pops the front task, calls
  `Run` and `Dispose`, and leaves only after the queue becomes empty.
- `skse64/Hooks_Threads.cpp:29-34` implements `AddTask` by entering that same
  critical section and pushing onto that same queue. There is no queue swap,
  snapshot count, frame marker, delayed-task flag, or next-pump queue.
- `skse64/Hooks_Threads.cpp:41-47` installs two task-pump call sites. This audit
  does not infer that either is a unique frame boundary.
- `skse64/Hooks_UI.cpp:46-71` likewise drains its live UI queue until empty.
  `AddUITask` at lines 74-80 supplies no next-pump guarantee; a missing UI manager
  can also prevent enqueue. It is not a qualified substitute for engine work.
- `skse64/PluginAPI.h:124-139` exposes task interface version 2 with only
  `AddTask` and `AddUITask`; neither returns scheduling acknowledgement.
- The consumed public `vendor/DevBenchAPI.h` has tool registration and listener
  callbacks, event publication, notices and time-scale operations, but no
  general next-frame main-thread callback facility. Its time-scale method is
  not an arbitrary task dispatcher.

The SKSE author's
[ICriticalSection wrapper](https://github.com/ianpatt/common/blob/205a8ffed2c31025ac13cfb49f74827f78e27d6f/common/ICriticalSection.h)
uses Win32 critical sections. Microsoft documents that an owning thread can
[enter the same critical section again](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-entercriticalsection#remarks)
without blocking. Thus the held lock does not make `AddTask` from `Run` defer;
its successor is visible to the current loop. This wrapper is supporting source
evidence, not a newly consumed runtime dependency.

The current [official SKSE64 source](https://github.com/ianpatt/skse64/blob/master/skse64/Hooks_Threads.cpp)
also drains until empty, although its locking differs. That corroboration does
not qualify every SE/AE binary. The exact VR archive/binary match establishes
the concrete blocker for this install.

## Scope of the gap

A continuation needs an evidenced boundary after a bounded amount of work;
ordinary `AddTask` provides no such boundary. A worker timer, alternating task
queues, sleeping inside a task, or a frame-counter spin cannot be assumed to
provide it. Engine-object dereferences must remain main-thread work.

One alternative for design review is event/read-assisted draining: schedule one
bounded dispatcher on incoming events, never schedule from the dispatcher, and
allow subsequent events or ordinary `probe.watch` reads/stops to drain backlog
using the request's existing budget. This reuses supported main-thread tasks,
but changes progress/delivery latency: an otherwise quiet backlog can wait until
the next poll. It is not an implementation or an approved replacement contract.

Any replacement must retain the 2,000-event bound including in-flight work,
immutable recipients/generations, once-only settlement, visible overflow and
failure accounting, and explicit visual sampling granularity. A read-assisted
design must keep the dispatcher ticket outstanding until that delegate runs or
is disposed, even if a read empties its queue first; request work must share its
single five-millisecond budget. Queue locks must not be held while submitting a
task or destroying a token whose settlement reacquires those locks.

No portable tests, DLL build, CTest, staging, slot acquisition, or runtime test
were run: product implementation was stopped at the explicitly required source
audit gate. Native behavior and performance acceptance remain open.

## Subsequent reviewed scheduling amendment

After this finding was reported, the follow-up spec was amended in `2a564b0` to
use event/read-assisted continuation. That preserves the source-audit conclusion:
no dispatcher calls AddTask to reschedule itself. Product implementation and
portable regression checks follow that amendment; see `CONTRACT.md`. Statements
above about no implementation/build describe the audit checkpoint, not the later
implementation or its separate verification. Runtime acceptance remains open.
