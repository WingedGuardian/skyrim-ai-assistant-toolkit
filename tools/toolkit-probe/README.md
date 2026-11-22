# ToolkitProbe (experimental)

Optional MIT SKSE plugin registering five read-only DevBench tools at `kDataLoaded`, after every plugin has finished `kPostLoad`.
DevBench initializes its interface listener during `kPostLoad`; requesting then
would depend on plugin callback order. Registration is attempted once at the
later data-loaded stage. DevBench build 13500 or newer is required; absent or older hosts register nothing.
The supported API has no registration preflight: `RegisterTool` returns false **after
replacing** an existing name. The plugin logs that collision; it cannot undo it.

This source has built as one Windows x64 DLL with SE, AE and VR enabled. No game
launch, installation or runtime qualification is implied by a successful build.
VR is the first qualification target; SE/AE remain experimental. Do not deploy
until the native contract and protected runtime tests are reviewed.

## Build

Use CMake 3.24+, MSVC with C++23, Ninja and vcpkg. Clone
`https://github.com/alandtse/CommonLibVR.git` at
`f82241e5daabfaadace7706bea14a202a7916e9c` (v9.3.0), then initialize its
submodules. Keep sources and build outputs beneath an ignored build directory.
The shipped vcpkg manifest pins its registry baseline. From a developer shell:

```powershell
cmake -S tools/toolkit-probe -B tools/toolkit-probe/build/plugin -G Ninja `
  -DCMAKE_BUILD_TYPE=Release -DCOMMONLIB_SOURCE_DIR=<absolute-clone-path> `
  -DCOMMONLIB_PREBUILT=OFF -DVCPKG_TARGET_TRIPLET=x64-windows-static-md `
  -DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreadedDLL `
  -DCMAKE_TOOLCHAIN_FILE=<vcpkg>/scripts/buildsystems/vcpkg.cmake
cmake --build tools/toolkit-probe/build/plugin --parallel 3
ctest --test-dir tools/toolkit-probe/build/plugin --output-on-failure
```

Portable logic and contract tests (no CommonLib or Windows SDK needed; requires
the same nlohmann-json package, available through vcpkg or a system CMake package):

```sh
cmake -S tools/toolkit-probe -B tools/toolkit-probe/build/portable -DPROBE_PLUGIN=OFF -Dnlohmann_json_DIR=<package-config-directory>
cmake --build tools/toolkit-probe/build/portable
ctest --test-dir tools/toolkit-probe/build/portable --output-on-failure
```

The parent build/packaging scripts bind a clean source commit to DLL hash before
creating an optional mod archive. Exact source blobs and all licenses are nested
in `source.zip` to avoid installer option groups or source files under Data.
Open that archive for `tools/toolkit-probe/README.md` and the repository `LICENSE`;
install only the outer mod archive. A development DLL is not a release receipt.

## Dependency provenance and licensing

`vendor/DevBenchAPI.*` is from `alandtse/devbench` commit
`cb0c0a9898f656e6df9697570026e7ef2705c44f`, `include/`. These three files are
MIT licensed independently of the GPL DevBench host. The 1.35 release tag does
not include this API; the toolkit's measured API recipe and source contract,
not an invented tag-matched copy, establish this dependency.

The CommonLib dependency is built from the pinned source, including its pinned
OpenVR submodule and hde64 dependency. `licenses/` carries the notices from this
build's vcpkg packages, CommonLib, hde64 and OpenVR. ToolkitProbe's original
source is covered by the repository MIT license. No upstream GPL host source is
compiled into ToolkitProbe.

See [CONTRACT.md](CONTRACT.md) for response shapes, coverage and lifetime limits.
