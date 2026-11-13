# libloot (third-party, GPL-3.0-or-later)

`wheels/` holds compiled builds of **libloot**, the library LOOT is built on, made by the LOOT Team
(https://github.com/loot/libloot). It is used only by `tools/loot_advisor.py`, which asks it for LOOT's view of the
load order and never writes one.

- **License:** libloot is licensed under the GNU General Public License, version 3 or later; its license text,
  unchanged from the source pack, is `COPYING` in this folder. The crates it is built from keep their own licenses;
  each one's license file is inside the source pack.
- **Exact version:** `PROVENANCE.json` names the upstream commit, the Rust and maturin versions, and the GitHub
  Actions run of `.github/workflows/libloot-wheels.yml` that built these wheels (that workflow is in the toolkit's
  GitHub repository, not in the release zip). `wheels/SHA256SUMS` lists their hashes.
- **Corresponding source:** `source/libloot-<version>-<commit>-source.tar.gz` is the complete source these wheels were
  built from: libloot at that commit plus every Rust crate it depends on (`cargo vendor`), so it builds with no
  network. `source/SHA256SUMS` lists its hash. `python tools/libloot/build.py build` compiles it.
- **No warranty.** libloot comes with no warranty, as the GPL states.
- The toolkit's own files are under its MIT license (`LICENSE`); the files in this folder that came from libloot are
  under libloot's. Only `loot_advisor.py` imports libloot, and nothing else in the toolkit needs it.
