#!/usr/bin/env python3
"""
Rica Report Launcher - Opens tedana output in Rica visualization

Usage:
    python open_rica_report.py [--port PORT] [--no-open] [--force-download]

This script checks for Rica files, downloads them if necessary, and then
starts a local server to visualize the ICA component analysis from this
tedana output directory.

Press Ctrl+C to stop the server when done.
"""

import argparse
import http.server
import json
import mimetypes
import os
import platform
import shutil
import socket
import sys
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import unquote

# Rica configuration
RICA_REPO_OWNER = "ME-ICA"
RICA_REPO_NAME = "rica"
RICA_GITHUB_API = (
    f"https://api.github.com/repos/{RICA_REPO_OWNER}/{RICA_REPO_NAME}/releases/latest"
)
RICA_FILES = ["index.html", "rica_server.py"]
RICA_PATH_ENV_VAR = "TEDANA_RICA_PATH"

# File patterns that Rica needs from tedana output
RICA_FILE_PATTERNS = [
    "_metrics.tsv",
    "_mixing.tsv",
    "_desc-ICA_components.nii.gz",
    "_mask.nii",
    "report.txt",
    "comp_",
    ".svg",
    "tedana_",
    "CrossComponent_metrics.json",
    # QC NIfTI files
    "T2starmap.nii",
    "S0map.nii",
    "rmse_statmap.nii",
    # Orig convention files
    "ica_components.nii.gz",
    "cross_component_metrics.json",
    "t2svG.nii",
    "s0vG.nii",
    "rmse.nii",
    # Decision tree files
    "decision_tree.json",
    "status_table.tsv",
    "registry.json",
]

# Ensure proper MIME types
mimetypes.add_type("application/gzip", ".gz")
mimetypes.add_type("text/tab-separated-values", ".tsv")


def get_rica_cache_dir():
    """Get platform-specific cache directory for Rica files."""
    system = platform.system()
    if system == "Linux":
        base_cache = Path.home() / ".cache"
    elif system == "Darwin":
        base_cache = Path.home() / "Library" / "Caches"
    elif system == "Windows":
        base_cache = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base_cache = Path.home() / ".cache"
    rica_cache = base_cache / "tedana" / "rica"
    rica_cache.mkdir(parents=True, exist_ok=True)
    return rica_cache


def validate_rica_path(rica_path):
    """Check if path contains required Rica files."""
    rica_path = Path(rica_path)
    if not rica_path.exists() or not rica_path.is_dir():
        return False
    return all((rica_path / f).exists() for f in RICA_FILES)


def get_cached_rica_version(cache_dir):
    """Get cached Rica version if available."""
    version_file = cache_dir / "VERSION"
    return version_file.read_text().strip() if version_file.exists() else None


def download_rica(force=False):
    """Download Rica from GitHub releases.

    Always checks GitHub for the latest version and downloads if:
    - force=True
    - Rica is not cached
    - Cached version is older than the latest release
    """
    cache_dir = get_rica_cache_dir()
    cached_version = get_cached_rica_version(cache_dir)

    # Always check GitHub for the latest version (unless we have no network)
    print("[Rica] Checking for latest version...")

    try:
        req = urllib.request.Request(
            RICA_GITHUB_API,
            headers={
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "tedana-rica-launcher",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            release_info = json.loads(response.read().decode("utf-8"))

        latest_version = release_info["tag_name"]
        assets = {}
        for asset in release_info.get("assets", []):
            if asset["name"] in RICA_FILES:
                assets[asset["name"]] = asset["browser_download_url"]

        if not assets:
            raise ValueError(f"No Rica assets found in release {latest_version}")

        # Check if we need to download
        if not force and cached_version == latest_version and validate_rica_path(cache_dir):
            print(f"[Rica] Using cached version {cached_version} (up to date)")
            return cache_dir

        # Download needed: either forced, new version available, or not cached
        if cached_version and cached_version != latest_version:
            print(f"[Rica] Updating from {cached_version} to {latest_version}...")
        else:
            print(f"[Rica] Downloading version {latest_version}...")

        for filename, url in assets.items():
            dest_path = cache_dir / filename
            req = urllib.request.Request(url, headers={"User-Agent": "tedana-rica-launcher"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                dest_path.write_bytes(resp.read())
            print(f"[Rica] Downloaded {filename}")

        (cache_dir / "VERSION").write_text(latest_version)
        print(f"[Rica] Successfully installed Rica {latest_version}")
        return cache_dir

    except (urllib.error.URLError, ValueError) as e:
        # Network error - fall back to cached version if available
        if validate_rica_path(cache_dir) and cached_version:
            print(f"[Rica] Warning: Could not check for updates ({e})")
            print(f"[Rica] Using cached version {cached_version}")
            return cache_dir
        raise RuntimeError(f"Failed to download Rica: {e}") from e


def setup_rica(output_dir, force_download=False):
    """Set up Rica files in the output directory.

    Always checks for the latest Rica version and downloads updates if available,
    unless TEDANA_RICA_PATH environment variable is set (user's explicit choice).
    """
    output_dir = Path(output_dir)
    rica_dir = output_dir / "rica"
    output_version_file = rica_dir / "VERSION"

    source_dir = None

    # Priority 1: Environment variable (user's explicit choice - no auto-update)
    env_path = os.environ.get(RICA_PATH_ENV_VAR)
    if env_path and validate_rica_path(env_path):
        source_dir = Path(env_path)
        print(f"[Rica] Using path from {RICA_PATH_ENV_VAR}: {source_dir}")

    # Priority 2: Download/update from GitHub (always checks for latest version)
    if source_dir is None:
        source_dir = download_rica(force=force_download)

    # Check if we need to update the output directory
    source_version = get_cached_rica_version(source_dir)
    output_version = None
    if output_version_file.exists():
        output_version = output_version_file.read_text().strip()

    is_up_to_date = (
        output_version and output_version == source_version and validate_rica_path(rica_dir)
    )
    if not force_download and is_up_to_date:
        print(f"[Rica] Using {output_version} from {rica_dir}")
        return rica_dir

    # Copy files to output (new install or update)
    rica_dir.mkdir(exist_ok=True)
    for filename in RICA_FILES + ["VERSION"]:
        src = source_dir / filename
        if src.exists():
            shutil.copy2(src, rica_dir / filename)

    if output_version and source_version and output_version != source_version:
        print(f"[Rica] Updated from {output_version} to {source_version} in {rica_dir}")
    else:
        print(f"[Rica] Installed {source_version} to {rica_dir}")
    return rica_dir


class RicaHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP handler with CORS support, cache control, and file listing endpoint."""

    # Data file extensions that should never be cached (fixes tedana#1323)
    # These files have identical names across different datasets, so caching
    # causes stale data to be served when switching between datasets
    # Note: .gz catches all gzip files including .nii.gz
    DATA_EXTENSIONS = (
        ".tsv", ".png", ".svg", ".json", ".txt",
        ".nii", ".gz",
    )

    def end_headers(self):
        # Restrict CORS to localhost origins only for security
        origin = self.headers.get("Origin")
        if origin and (
            origin.startswith("http://localhost")
            or origin.startswith("http://127.0.0.1")
            or origin.startswith("http://[::1]")
            or origin.startswith("https://localhost")
            or origin.startswith("https://127.0.0.1")
            or origin.startswith("https://[::1]")
        ):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # Add cache-control headers to prevent browser caching
        # This is critical for:
        # 1. Rica core files (.html, .py) - ensures updated versions are served
        # 2. Data files (.tsv, .png, .svg, etc.) - prevents stale data when
        #    switching between different tedana output directories (#1323)
        path = unquote(self.path) if hasattr(self, 'path') else ""
        is_rica_core = "/rica/" in path and (path.endswith(".html") or path.endswith(".py"))
        is_data_file = any(path.endswith(ext) for ext in self.DATA_EXTENSIONS)
        if is_rica_core or is_data_file:
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        path = unquote(self.path)
        if path == "/api/files":
            self.send_file_list()
        else:
            super().do_GET()

    def send_file_list(self):
        files = []
        cwd = Path.cwd()

        # Limit traversal depth to avoid performance issues on large directories
        max_depth = 3

        for root, dirs, filenames in os.walk(cwd):
            root_path = Path(root)
            rel_root = root_path.relative_to(cwd)

            # Stop descending further once max depth is reached, but still process files
            if len(rel_root.parts) >= max_depth:
                dirs[:] = []

            for name in filenames:
                components_suffix = "_components.nii.gz"
                is_components_file = name.endswith(components_suffix)
                is_stat_z = is_components_file and "stat-z" in name
                is_echo_component = is_components_file and "echo-" in name

                # A "primary" ICA components file has exactly `_desc-ICA` before
                # the suffix, e.g., `*_desc-ICA_components.nii.gz`. Derived files
                # like `*_desc-ICAAccepted_components.nii.gz` will not match.
                has_exact_desc_ica = False
                if is_components_file:
                    prefix = name[: -len(components_suffix)]
                    has_exact_desc_ica = prefix.endswith("_desc-ICA")

                allow_components_file = (
                    not is_components_file
                    or has_exact_desc_ica
                    or "ica_components.nii.gz" in name
                )

                if (
                    any(p in name for p in RICA_FILE_PATTERNS)
                    and not is_stat_z
                    and not is_echo_component
                    and allow_components_file
                ):
                    f = root_path / name
                    files.append(f.relative_to(cwd).as_posix())

        # Don't expose full path for security
        response_data = {"files": sorted(files), "path": ".", "count": len(files)}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response_data, indent=2).encode("utf-8"))

    def log_message(self, format, *args):
        try:
            msg = str(args[0]) if args else ""
            if "/api/files" in msg:
                print("[Rica] File list requested")
            elif "GET" in msg and len(args) > 1 and not str(args[1]).startswith("2"):
                print(f"[Rica] {msg} - {args[1]}")
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(
        description="Open Rica report to visualize tedana ICA components"
    )
    parser.add_argument("--port", type=int, default=8000, help="Port to serve on")
    parser.add_argument("--no-open", action="store_true", help="Don't auto-open browser")
    parser.add_argument("--force-download", action="store_true", help="Force re-download Rica")
    args = parser.parse_args()

    script_dir = Path(__file__).parent.resolve()

    # Set up Rica (download if needed)
    try:
        setup_rica(script_dir, force_download=args.force_download)
    except RuntimeError as e:
        print(f"Error: {e}")
        print("Please check your internet connection and try again.")
        sys.exit(1)

    # Verify Rica is ready
    rica_index = script_dir / "rica" / "index.html"
    if not rica_index.exists():
        print(f"Error: Rica not found at {rica_index}")
        sys.exit(1)

    os.chdir(script_dir)

    # Find free port
    port = args.port
    for _ in range(10):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("", port))
            sock.close()
            break
        except OSError:
            port += 1
    else:
        print(f"Error: Could not find free port starting from {args.port}")
        sys.exit(1)

    # Start server
    try:
        with http.server.HTTPServer(("", port), RicaHandler) as httpd:
            url = f"http://localhost:{port}/rica/index.html"
            print()
            print("=" * 60)
            print("Rica - ICA Component Visualization")
            print("=" * 60)
            print()
            print(f"Server running at: http://localhost:{port}")
            print(f"Rica interface:    {url}")
            print(f"Serving files from: {script_dir}")
            print()
            print("Press Ctrl+C to stop the server")
            print()

            if not args.no_open:
                webbrowser.open(url)

            httpd.serve_forever()

    except KeyboardInterrupt:
        print("\nServer stopped.")
        sys.exit(0)
    except OSError as e:
        if "Address already in use" in str(e):
            print(f"Error: Port {port} is already in use.")
            print(f"Try: python open_rica_report.py --port {port + 1}")
        else:
            raise


if __name__ == "__main__":
    main()
