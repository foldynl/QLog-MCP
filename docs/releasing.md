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

## Version format

The version comes from `git describe` through `versioning.py`.

| Git state | Package and AppImage version |
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

The `AppImage` workflow builds native `x86_64` and `aarch64` AppImages. For a tag push
only, it then creates a GitHub Release containing both AppImages and their SHA-256
files. Its release notes contain the abbreviated Git log since the preceding version
tag. The first tag uses the complete history because there is no preceding tag.

The workflow run, jobs, Actions artifacts, and files all use the same derived version.
