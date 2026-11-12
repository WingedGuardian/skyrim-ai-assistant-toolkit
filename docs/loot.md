# LOOT advisor (read-only)

`tools/loot_advisor.py` tells the agent what LOOT would say about the active load order. It changes nothing: it
never writes `plugins.txt`, `loadorder.txt` or LOOT's own files, and it never calls libloot's order-writing
functions (a test runs it against a fake libloot that fails on any such call, and a mutation test proves that test
goes red when one is added).

```bash
python tools/loot_advisor.py            # summary
python tools/loot_advisor.py --json     # everything, for the agent
python tools/loot_advisor.py --offline  # the last fetched masterlist, no network
```

## What it reports

- **order**: the order LOOT would sort the active plugins into, and the fewest plugins that order moves (the rest
  keep their relative order), each with the plugin it would land after.
- **issues**: the masterlist's messages for the active plugins and its general messages; dirty plugins (ITM,
  deleted-reference and deleted-navmesh counts, and the cleaner LOOT names); requirements that are missing (a plugin
  counts when active, any other file when it is on disk under Data or an enabled MO2 mod folder); incompatibilities
  where both sides are active.

Exit 0 when LOOT has nothing to add, 1 with findings, 2 when it could not check (the reason is printed; it is never
reported as clean).

## A move is advice

Wrye Bash or the mod manager owns the order. On a save you are keeping, moving or removing plugins can break it: read
`## My preferences` in `KNOWLEDGEBASE.local.md` before acting on a move, and make the change in the tool that owns
the order.

## The masterlist

LOOT's masterlist and prelude are fetched from LOOT's GitHub repositories at run time: the newest `vX.Y` branch this
libloot can read (the report names the branch and commit). They are cached in `.skyguard/loot/masterlists/` (the
current and previous copy). When the fetch fails, the last copy is used and the report says CACHED with the reason;
with no copy at all the result is could-not-check.

## libloot

libloot is the library LOOT is built on (GPL-3.0-or-later, by the LOOT Team). The toolkit ships it as wheels for
Python 3.10-3.13 on Windows x64 in `tools/libloot/wheels/`, built by `.github/workflows/libloot-wheels.yml` from the
upstream commit in `tools/libloot/PROVENANCE.json`, with the complete corresponding source in `tools/libloot/source/`
(see `tools/libloot/NOTICE.md`). The loader refuses a wheel that is not listed in `SHA256SUMS` or does not match it,
and re-checks the unpacked copy against the wheel's own file hashes on every run.

On Linux, macOS or another Python, build it from the shipped source (every crate is vendored, so no network):

```bash
python -m pip install --only-binary=:all: --require-hashes -r tools/libloot/build-requirements.lock
python tools/libloot/build.py build      # needs Rust (https://rustup.rs); about a minute
```

The build goes to `.skyguard/loot/built/`, which the loader prefers over the shipped wheels.

## Limits

- It reads plugin headers and the masterlist; it does not run LOOT's GUI or apply user metadata from LOOT's
  `userlist.yaml`.
- A masterlist message is LOOT's community data, not a measurement of your install: check it before acting.
