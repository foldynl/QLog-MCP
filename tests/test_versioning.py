"""Tests for Git-derived project versions."""

from pathlib import Path
from runpy import run_path

import pytest

format_git_version = run_path(Path(__file__).parents[1] / "versioning.py")[
    "format_git_version"
]


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("v0.2.0-0-gabcdef", "0.2.0"),
        ("v0.2.0-3-gabcdef", "0.2.3+gabcdef"),
        ("v0.2.4-3-gabcdef-dirty", "0.2.7+gabcdef.dirty"),
        ("abcdef", "0.1.0+gabcdef"),
    ],
)
def test_formats_git_description(description: str, expected: str) -> None:
    assert format_git_version(description) == expected
