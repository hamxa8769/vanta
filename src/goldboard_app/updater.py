"""In-app updater for Vanta (Phase 4) — pure logic, no Qt, no network in tests.

Same pattern as Muuk's updater: check the GitHub releases feed for
``hamxa8769/vanta``, compare against the running version, download the
Windows zip, verify its SHA-256, then hand over to a small Windows
``update.bat`` that swaps the files and restarts the app. Config lives
in %APPDATA%/GoldBoard (outside the install dir), so it is preserved
across updates automatically.

The repo does NOT exist yet — every entry point handles a 404
gracefully and reports "no release channel yet" instead of crashing.
Nothing in this module downloads or installs anything unless the
caller explicitly calls :func:`download_asset` / :func:`install_update`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import requests

REPO = "hamxa8769/vanta"
RELEASES_API_URL = f"https://api.github.com/repos/{REPO}/releases"
NO_CHANNEL_MESSAGE = "no release channel yet"

# Asset preference for the Windows build (first match wins).
ASSET_SUFFIXES = (".zip", ".exe")


class UpdateError(Exception):
    """Raised when a download / verify / install step fails."""


# ------------------------------------------------------------------ versions

_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+){0,3})")


def parse_version(tag: str | None) -> tuple[int, ...] | None:
    """Parse ``v0.1.0`` / ``0.1.0`` into (0, 1, 0); None if unparseable."""
    if not tag:
        return None
    match = _VERSION_RE.match(str(tag).strip())
    if not match:
        return None
    try:
        return tuple(int(p) for p in match.group(1).split("."))
    except ValueError:
        return None


def _pad(version: tuple[int, ...], length: int) -> tuple[int, ...]:
    return version + (0,) * (length - len(version))


def is_newer(current: str, latest: str) -> bool:
    """True when ``latest`` is a strictly newer version than ``current``."""
    cur = parse_version(current)
    lat = parse_version(latest)
    if cur is None or lat is None:
        return False
    n = max(len(cur), len(lat))
    return _pad(lat, n) > _pad(cur, n)


# ------------------------------------------------------------- release check

def _pick_asset(release: dict) -> dict | None:
    """Best downloadable asset for Windows, or None."""
    assets = release.get("assets") or []
    for suffix in ASSET_SUFFIXES:
        for asset in assets:
            name = str(asset.get("name", "")).lower()
            if name.endswith(suffix) and "windows" in name:
                return asset
    for suffix in ASSET_SUFFIXES:
        for asset in assets:
            if str(asset.get("name", "")).lower().endswith(suffix):
                return asset
    return assets[0] if assets else None


def _pick_sha256_asset(release: dict) -> dict | None:
    for asset in release.get("assets") or []:
        if str(asset.get("name", "")).lower().endswith(".sha256"):
            return asset
    return None


def check_for_update(
    current_version: str, releases_json: list | dict | None
) -> dict | None:
    """Pick the newest applicable release newer than ``current_version``.

    ``releases_json`` is the decoded GitHub ``/releases`` response (a
    list), or a ``{"message": ...}`` error object, or None. Returns a
    dict describing the update, or None when there is no newer release
    (including: repo not created yet / 404 / empty feed / only drafts
    and prereleases). Never raises on malformed input.
    """
    if not isinstance(releases_json, list):
        return None
    best: dict | None = None
    best_version: tuple[int, ...] | None = None
    for release in releases_json:
        if not isinstance(release, dict):
            continue
        if release.get("draft") or release.get("prerelease"):
            continue
        version = parse_version(release.get("tag_name"))
        if version is None:
            continue
        if not is_newer(current_version, release.get("tag_name", "")):
            continue
        if best_version is None or version > best_version:
            best, best_version = release, version
    if best is None:
        return None
    asset = _pick_asset(best)
    sha_asset = _pick_sha256_asset(best)
    return {
        "version": (best.get("tag_name") or "").lstrip("v"),
        "tag": best.get("tag_name"),
        "name": best.get("name") or best.get("tag_name"),
        "html_url": best.get("html_url"),
        "published_at": best.get("published_at"),
        "asset_name": asset.get("name") if asset else None,
        "asset_url": asset.get("browser_download_url") if asset else None,
        "asset_size": asset.get("size") if asset else None,
        "sha256_asset_name": sha_asset.get("name") if sha_asset else None,
        "sha256_asset_url": (
            sha_asset.get("browser_download_url") if sha_asset else None
        ),
    }


def fetch_releases(
    repo: str = REPO, timeout: float = 15.0
) -> tuple[list | dict | None, str | None]:
    """GET the GitHub releases feed.

    Returns ``(releases_json, None)`` on success, or ``(None, error)``.
    A 404 (repo not created yet) returns the "no release channel yet"
    message — it is an expected state, not a crash.
    """
    url = f"https://api.github.com/repos/{repo}/releases"
    try:
        resp = requests.get(
            url,
            headers={"Accept": "application/vnd.github+json"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return None, f"update check failed (network): {exc}"
    if resp.status_code == 404:
        return None, f"{NO_CHANNEL_MESSAGE} (GitHub repo {repo} not found: 404)"
    if resp.status_code != 200:
        return None, f"update check failed (GitHub API HTTP {resp.status_code})"
    try:
        return resp.json(), None
    except ValueError:
        return None, "update check failed (GitHub API returned invalid JSON)"


def check_for_update_live(
    current_version: str, repo: str = REPO, timeout: float = 15.0
) -> tuple[dict | None, str | None]:
    """One call: fetch the feed and pick an update, if any.

    Returns ``(update_dict_or_None, error_or_None)``. ``error`` is set
    only for real failures (network / API); "no release channel yet"
    and "no newer release" both return ``(None, None)``-style results
    with the reason in ``error`` for the channel case only when the
    caller wants to display it — here: channel-missing is returned as
    an informational error string, no update is available either way.
    """
    releases, error = fetch_releases(repo, timeout=timeout)
    if error is not None:
        return None, error
    return check_for_update(current_version, releases), None


# -------------------------------------------------------------- verification

def verify_sha256(path: str | Path, expected_hex: str) -> bool:
    """True when the file's SHA-256 matches ``expected_hex`` (case-insensitive)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower() == expected_hex.strip().lower()


def parse_sha256_text(text: str) -> str | None:
    """First hex token of a ``.sha256`` file (``<hex>  <filename>``)."""
    match = re.search(r"\b([0-9a-fA-F]{64})\b", text or "")
    return match.group(1).lower() if match else None


# ------------------------------------------------------------------ download

def download_asset(
    url: str,
    dest: str | Path,
    expected_sha256: str | None = None,
    timeout: float = 120.0,
    progress_cb=None,
) -> Path:
    """Stream-download ``url`` to ``dest``; verify SHA-256 when given.

    Raises :class:`UpdateError` on HTTP failure or hash mismatch.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(url, stream=True, timeout=timeout) as resp:
            if resp.status_code != 200:
                raise UpdateError(f"download failed (HTTP {resp.status_code}): {url}")
            total = int(resp.headers.get("content-length") or 0)
            done = 0
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    done += len(chunk)
                    if progress_cb is not None:
                        progress_cb(done, total)
    except requests.RequestException as exc:
        raise UpdateError(f"download failed (network): {exc}") from exc
    if expected_sha256 and not verify_sha256(dest, expected_sha256):
        dest.unlink(missing_ok=True)
        raise UpdateError("download failed (SHA-256 mismatch — file discarded)")
    return dest


def fetch_expected_sha256(sha256_url: str, timeout: float = 15.0) -> str | None:
    """Download a ``.sha256`` sidecar and return its hex digest, or None."""
    try:
        resp = requests.get(sha256_url, timeout=timeout)
        if resp.status_code != 200:
            return None
        return parse_sha256_text(resp.text)
    except requests.RequestException:
        return None


# ------------------------------------------------------------------- install

def build_update_script(
    install_dir: str | Path, zip_path: str | Path, exe_name: str = "Vanta.exe"
) -> str:
    """Windows ``update.bat`` text: swap in the new zip, restart the app.

    The running exe cannot overwrite itself, so the app writes this
    script next to the exe and launches it on exit. The script waits
    for the app to close, extracts the zip over the install dir, and
    restarts the exe. Config is NOT in the install dir (it lives in
    %APPDATA%/GoldBoard), so it is preserved automatically; the script
    also never deletes ``*.local.json`` files.
    """
    install_dir = Path(install_dir)
    zip_path = Path(zip_path)
    exe_path = install_dir / exe_name
    lines = [
        "@echo off",
        "rem Vanta self-update — generated by the in-app updater. Do not run by hand.",
        "setlocal",
        f'set "INSTALL_DIR={install_dir}"',
        f'set "ZIP={zip_path}"',
        f'set "EXE={exe_path}"',
        "rem Wait for the running app to exit so its files can be replaced.",
        ":wait",
        'tasklist /FI "IMAGENAME eq Vanta.exe" 2>NUL | find /I "Vanta.exe" >NUL',
        "if %ERRORLEVEL%==0 ( timeout /t 1 /nobreak >NUL & goto wait )",
        "rem Extract the new build over the install dir (config lives in",
        "rem %APPDATA%\\GoldBoard, so nothing the user configured is touched).",
        'powershell -NoProfile -ExecutionPolicy Bypass -Command '
        '"Expand-Archive -LiteralPath \\"%ZIP%\\" '
        '-DestinationPath \\"%INSTALL_DIR%\\" -Force"',
        'if exist "%EXE%" start "" "%EXE%"',
        "endlocal",
        "del \"%~f0\"",
    ]
    return "\r\n".join(lines) + "\r\n"


def install_update(
    zip_path: str | Path,
    install_dir: str | Path | None = None,
    exe_name: str = "Vanta.exe",
) -> Path:
    """Write the update script and launch it (Windows only).

    Returns the path of the written ``update.bat``. The caller should
    exit the app immediately after this returns so the script can
    replace the files. Raises :class:`UpdateError` off Windows.
    """
    if os.name != "nt":
        raise UpdateError("in-app install is Windows-only on this build")
    install_dir = Path(install_dir) if install_dir else Path(sys.executable).parent
    script = build_update_script(install_dir, zip_path, exe_name)
    bat_path = install_dir / "update.bat"
    bat_path.write_text(script, encoding="utf-8")
    subprocess.Popen(
        ["cmd", "/c", str(bat_path)],
        cwd=str(install_dir),
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return bat_path


def load_version_file(path: str | Path | None = None) -> str:
    """Read the VERSION file next to the package, or '0.0.0' if missing."""
    candidates = []
    if path is not None:
        candidates.append(Path(path))
    here = Path(__file__).resolve()
    candidates += [here.parents[2] / "VERSION", Path.cwd() / "VERSION"]
    for candidate in candidates:
        try:
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8").strip()
                if parse_version(text):
                    return text
        except OSError:
            continue
    return "0.0.0"


def dump_state() -> str:
    """Debug helper — not used by the UI."""
    return json.dumps({"repo": REPO, "api": RELEASES_API_URL}, indent=2)
