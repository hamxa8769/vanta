"""Updater tests — pure logic, no Qt, no network."""

import hashlib

import pytest

from goldboard_app.updater import (
    NO_CHANNEL_MESSAGE,
    build_update_script,
    check_for_update,
    is_newer,
    parse_sha256_text,
    parse_version,
    verify_sha256,
)

RELEASES = [
    {
        "tag_name": "v0.2.0",
        "name": "Vanta v0.2.0",
        "html_url": "https://github.com/hamxa8769/vanta/releases/tag/v0.2.0",
        "published_at": "2026-11-22T00:00:00Z",
        "draft": False,
        "prerelease": False,
        "assets": [
            {"name": "Vanta-0.2.0-Windows.zip", "size": 123456,
             "browser_download_url": "https://example.invalid/Vanta-0.2.0-Windows.zip"},
            {"name": "Vanta-0.2.0-Windows.zip.sha256", "size": 90,
             "browser_download_url": "https://example.invalid/Vanta-0.2.0-Windows.zip.sha256"},
        ],
    },
    {
        "tag_name": "v0.1.1",
        "name": "Vanta v0.1.1",
        "html_url": "https://github.com/hamxa8769/vanta/releases/tag/v0.1.1",
        "published_at": "2026-11-21T12:00:00Z",
        "draft": False,
        "prerelease": False,
        "assets": [],
    },
]


def test_parse_version():
    assert parse_version("v0.1.0") == (0, 1, 0)
    assert parse_version("0.2.0") == (0, 2, 0)
    assert parse_version("v1.10.3") == (1, 10, 3)
    assert parse_version("") is None
    assert parse_version(None) is None
    assert parse_version("not-a-version") is None


def test_is_newer():
    assert is_newer("0.1.0", "0.2.0")
    assert is_newer("0.1.0", "v0.1.1")
    assert not is_newer("0.2.0", "0.2.0")
    assert not is_newer("0.2.0", "0.1.0")
    assert not is_newer("0.1.0", "garbage")
    assert is_newer("0.1", "0.1.1")  # unequal lengths compare correctly


def test_check_for_update_picks_newest_release_with_asset():
    update = check_for_update("0.1.0", RELEASES)
    assert update is not None
    assert update["version"] == "0.2.0"
    assert update["tag"] == "v0.2.0"
    assert update["asset_name"] == "Vanta-0.2.0-Windows.zip"
    assert update["asset_url"] == "https://example.invalid/Vanta-0.2.0-Windows.zip"
    assert update["sha256_asset_url"] == (
        "https://example.invalid/Vanta-0.2.0-Windows.zip.sha256")


def test_check_for_update_none_when_current():
    assert check_for_update("0.2.0", RELEASES) is None
    assert check_for_update("0.3.0", RELEASES) is None


def test_check_for_update_skips_drafts_and_prereleases():
    releases = [
        {"tag_name": "v9.9.9", "draft": True, "prerelease": False, "assets": []},
        {"tag_name": "v9.9.8", "draft": False, "prerelease": True, "assets": []},
    ]
    assert check_for_update("0.1.0", releases) is None


def test_check_for_update_handles_missing_repo_gracefully():
    # Repo does not exist yet: GitHub returns {"message": "Not Found"}.
    assert check_for_update("0.1.0", {"message": "Not Found"}) is None
    assert check_for_update("0.1.0", None) is None
    assert check_for_update("0.1.0", []) is None
    assert NO_CHANNEL_MESSAGE == "no release channel yet"


def test_verify_sha256(tmp_path):
    data = b"vanta-test-payload"
    target = tmp_path / "file.bin"
    target.write_bytes(data)
    expected = hashlib.sha256(data).hexdigest()
    assert verify_sha256(target, expected)
    assert verify_sha256(target, expected.upper())  # case-insensitive
    assert not verify_sha256(target, "0" * 64)


def test_parse_sha256_text():
    digest = "a" * 64
    assert parse_sha256_text(f"{digest}  Vanta-0.2.0-Windows.zip") == digest
    assert parse_sha256_text("not a hash") is None


def test_build_update_script_preserves_config_and_restarts(tmp_path):
    install_dir = tmp_path / "Vanta"
    zip_path = tmp_path / "Vanta-0.2.0-Windows.zip"
    script = build_update_script(install_dir, zip_path)
    assert "Expand-Archive" in script
    assert str(install_dir) in script
    assert str(zip_path) in script
    assert "Vanta.exe" in script
    # Config lives outside the install dir — the script must not touch it.
    assert "APPDATA" in script
    assert "config.json" not in script.replace("APPDATA", "")
