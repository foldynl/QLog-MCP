# Building and releasing

This page is for maintainers. Operators who only want to use the server should start
with the [README](../README.md).

## Build an AppImage locally

On Linux, run:

```bash
packaging/appimage/build-appimage.sh
```

The script builds for the current native architecture and creates a versioned file such
as `dist/qlog-mcp-0.2.0-x86_64.AppImage`. It verifies that the AppImage's
`--version` output matches its filename and creates a matching SHA-256 file.

## Build a Windows executable locally

On Windows x64 with `uv` and PowerShell 7.4 or newer, run:

```powershell
pwsh -File packaging/windows/build-exe.ps1
```

The script uses Python 3.12 and pinned PyInstaller to create a single executable such as
`dist/qlog-mcp-0.2.0-windows-x86_64.exe`. Python and the application dependencies from
`uv.lock` are included; users do not need to install Python or `uv`. The executable
retains the console and standard input/output required by MCP clients.

Before writing the matching SHA-256 file, the script verifies `--version` and starts an
MCP client session against the executable from a temporary directory. Listing tools
must succeed with a nonexistent database path and must not create that database.

## Version format

The version comes from `git describe` through `versioning.py`.

| Git state | Package, AppImage, and Windows EXE version |
| --- | --- |
| Exact `v0.2.0` tag | `0.2.0` |
| Three commits after `v0.2.0` | `0.2.3+gabcdef` |
| Tracked working-tree changes | Adds `.dirty` |
| No version tag yet | `0.1.0+g<commit>` |

`uv` includes the current commit and tags in its cache key, so an editable installation
is rebuilt when the Git-derived version changes.

## Publish a release

First commit and push the intended version of the workflow to `main`. Then tag that
commit and push the tag:

```bash
git push origin main
git tag -a v0.2.0 -m "QLog MCP 0.2.0"
git push origin v0.2.0
```

The `CI and packages` workflow runs dependency, package, pytest, and Ruff checks on
pull requests to `main`, pushes to `main`, `v*` tags, and manual dispatch. After those
checks pass, events other than pull requests build native `x86_64` and `aarch64`
AppImages and a Windows x64 executable, and upload them as Actions artifacts.
For a tag push only, it then creates a GitHub Release containing both AppImages, the
Windows executable, and their SHA-256 files. All packaging jobs must pass before a
release is created. Its release notes contain the abbreviated Git log since the
preceding version tag. The first tag uses the complete history because there is no
preceding tag.

The workflow run, jobs, Actions artifacts, and files all use the same derived version.
