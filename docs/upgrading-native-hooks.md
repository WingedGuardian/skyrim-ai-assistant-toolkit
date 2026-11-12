# Reviewed upgrade for installs with their own hook handlers

> Most installs do not need this page: follow "How do I update the toolkit?" in [README.md](../README.md). This
> page is for an install that has its own pre-tool hook handlers (for example a `.claude/hooks/protect-save-access.py`
> you wrote), which extracting a new version over the install would replace.

The commands below are subcommands of the NEW toolkit's entry, run from the folder you unpacked it into, with
`--installed-root` naming your Skyrim folder. `<new folder>` is the zip's one inner folder,
`skyrim-ai-assistant-toolkit-<version>/` (the one with `setup.sh` and `tools/` directly inside it), not the folder
you unpacked the zip into; the candidate's paths are relative to it:

```bash
python "<new folder>/tools/agent-harness.py" upgrade-preview --installed-root "<Skyrim folder>" --candidate "<scratch folder>/candidate.json"
```

`--candidate FILE` is an inventory of the new version: `{"v":1,"files":{"relative/path":"sha256"}}`. The release
does not ship one; make it from the unpacked folder (this hashes every file in it), into a folder outside both
trees:

```bash
cd "<new folder>" && python -c "import hashlib,json,pathlib; r=pathlib.Path('.'); print(json.dumps({'v':1,'files':{p.relative_to(r).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(r.rglob('*')) if p.is_file()}}))" > "<scratch folder>/candidate.json"
```

## Personal save policies (retired in 4.1)

Since 4.1 no save policy is consulted: the save vault keeps every version of every save
([save-vault.md](save-vault.md)), so a personal save guard such as an installed
`protect-save-access.py` is neither registered nor read. An upgrade keeps its files as local files, untouched;
`.modding-agent-harness/local-policies.json` is no longer read, and `upgrade-stage --integrate-local-policy` refuses
("this profile provides no qualified local-policy migration"). If the personal guard is still registered as a native
pre-tool handler, it is an unresolved handler below: remove its registration yourself, or keep the file kept.

`upgrade-preview --installed-root DIR --candidate FILE [--previous FILE]` compares version 1 inventories
(`{"v":1,"files":{"relative/path":"sha256"}}`) without writing anything.
Unknown or modified installed bytes are collisions to preserve; previous
ownership is accepted only for matching bytes. A preview does not approve an
upgrade or establish host interception.

`upgrade-stage --installed-root DIR --source DIR --candidate FILE --destination DIR`
prepares a private review bundle with independent candidate and rollback copies,
merged project settings and local-setting review. Add `--previous FILE` and exact
verified `--owned-command COMMAND` values for lifecycle handlers where available.
Pre-tool retirement uses complete groups: exact current candidate groups and
unchanged files matching the previous inventory qualify automatically. Otherwise
`--owned-pre-groups FILE` supplies complete groups scoped to their native definition
file, using the [native hook formats](#native-hook-formats) below,
from verified previous definitions. Matcher, timeout,
Windows command and asynchronous changes remain custom; command text alone is
insufficient. Existing personal save
guard sources are retained; `--local-file PATH` includes other local policy files.
This command changes no installed files and performs no deployment or hook review.
The bundle contains private notes/settings and must stay out of release artifacts.

Add `--render-config FILE` to render the independently copied instructions and
hook transports for the destination before review. The source and installed
files stay unchanged. The JSON object supplies actual absolute paths, for example:

```json
{
  "v": 1,
  "config_dir": "C:/Profiles/Skyrim VR",
  "loadorder_dir": "C:/LoadOrder/Skyrim VR",
  "skyrim_folder": "Skyrim VR",
  "python": "C:/Tools/Python/python.exe",
  "bash": "C:/Program Files/Git/bin/bash.exe",
  "mod_manager": {"kind": "stock"}
}
```

The destination supplies the toolkit root used by hook definitions. Instructions
use the retained `SKYRIM_GAME_ROOT`, or the optional absolute `game_root` field;
without either they use the destination's usual in-game layout. This distinction
keeps a separate worktree from being mistaken for the game. `stock` covers Vortex/manual deployment.
For MO2, use `kind: "mo2"` and supply `instance`, `profile`, `mods`, `overwrite`
and `profile_dir`; all except the profile name are absolute paths. Both config
and load-order directories must name that profile. Values conflicting with
retained installation paths refuse. The existing Codex definition renderer keeps
its command normalization; unsafe roots refuse. Interpreter literals use their
shell's quoting, and Unicode PowerShell scripts carry an explicit UTF-8 marker
for Windows PowerShell 5.1. Unresolved placeholders or oversized agent
instructions refuse application before installed writes. This renders selected
candidate files; it does not select a complete payload or establish host trust.

After reviewing the bundle, `upgrade-apply --installed-root DIR --bundle DIR
--review-sha256 SHA256 --choices FILE --quiescent` applies its selected files.
SHA256 is the hash of the exact `complete.json` reviewed. The choices file is a
JSON object mapping every collision path to `keep` or `candidate`; project
hook definitions for both supported native hosts instead select `merged_settings`
when replacing existing definitions. Omit `--choices`
only when no collision needs a choice. Custom pre-tool handlers must first be
integrated into the ordered dispatcher and reviewed; an unresolved handler
in either native configuration refuses application, even when its file is kept.
Local policy or configuration changes invalidate review. Restage older bundles
that lack the combined native-settings observations.
Stop host/editor writers before using `--quiescent`: the flag asserts that
condition and does not stop them. The upgrade lock coordinates upgraders only;
replacing existing files cannot exclude arbitrary foreign writers atomically.
Read-only installed targets refuse during preflight; their protection is retained.
The receipt gives an operation ID and permanent rollback journal. Exit 2 is a
refusal; exit 1 reports a partial failure with its journal and published paths.
Inspect that evidence before recovery. `upgrade-rollback ID --installed-root
DIR --quiescent` restores previous files only when the upgrade's published
bytes and identity still match, retaining new files in the journal. Local notes
remain preserved. Installation and rollback both require separate host hook
review before claiming enforcement.

## Native hook formats

These are native configuration formats for Claude Code and Codex, independent of the agent performing the
upgrade.
`--owned-pre-groups FILE` reads a versioned object whose `files` maps
`.claude/settings.json` and/or `.codex/hooks.json` to lists of complete verified
previous `PreToolUse` groups. Each group must match in full, including matcher,
handler order, command, Windows command, timeout and asynchronous attributes.
Do not infer ownership from a familiar command name or copy a modified group
into that file and describe it as a verified previous definition.

For an existing native definition, replacing it uses an explicit
`merged_settings` choice. The core reviews both definition files and also
`.claude/settings.local.json`. Unresolved pre-tool handlers in any reviewed
configuration refuse application even when that file is kept. Unknown post-tool
handlers and unrelated settings remain in the merged proposal.
