"""Derive the project version from ``git describe``."""

from __future__ import annotations

import re
import subprocess
from email.parser import Parser
from pathlib import Path

_BOOTSTRAP_VERSION = "0.1.0"
_DESCRIBE_PATTERN = re.compile(
    r"^v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)-"
    r"(?P<distance>\d+)-g(?P<commit>[0-9a-f]+)(?P<dirty>-dirty)?$"
)
_UNTAGGED_PATTERN = re.compile(r"^(?P<commit>[0-9a-f]+)(?P<dirty>-dirty)?$")


def format_git_version(description: str) -> str:
    """Convert Git's long description to the project's PEP 440 version."""
    match = _DESCRIBE_PATTERN.fullmatch(description)
    if match:
        base = ".".join(match.group("major", "minor", "patch"))
        distance = int(match.group("distance"))
        if distance == 0 and not match.group("dirty"):
            return base

        patch = int(match.group("patch")) + distance
        release = f"{match.group('major')}.{match.group('minor')}.{patch}"
        dirty = ".dirty" if match.group("dirty") else ""
        return f"{release}+g{match.group('commit')}{dirty}"

    match = _UNTAGGED_PATTERN.fullmatch(description)
    if match:
        dirty = ".dirty" if match.group("dirty") else ""
        return f"{_BOOTSTRAP_VERSION}+g{match.group('commit')}{dirty}"

    raise ValueError(
        f"Unsupported Git description {description!r}; expected a vMAJOR.MINOR.PATCH tag"
    )


def project_version(root: Path) -> str:
    """Return the Git version, or preserved metadata when building from an sdist."""
    try:
        result = subprocess.run(
            [
                "git",
                "describe",
                "--tags",
                "--long",
                "--dirty",
                "--always",
                "--match",
                "v[0-9]*.[0-9]*.[0-9]*",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        metadata_file = root / "PKG-INFO"
        if metadata_file.is_file():
            metadata = Parser().parsestr(metadata_file.read_text(encoding="utf-8"))
            if version := metadata.get("Version"):
                return version
        raise RuntimeError("Cannot determine the project version from Git or PKG-INFO") from None

    return format_git_version(result.stdout.strip())


__version__ = project_version(Path(__file__).parent)


if __name__ == "__main__":
    print(__version__)
