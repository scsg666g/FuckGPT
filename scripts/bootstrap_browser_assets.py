"""Download browser runtime assets used by local automation.

Run this after installing Python requirements so browser registration tasks do
not have to fetch browser binaries or GeoIP data mid-task.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Iterable


def run_command(args: list[str]) -> None:
    print(f"$ {' '.join(args)}", flush=True)
    subprocess.run(args, check=True)


def install_playwright(with_deps: bool) -> None:
    args = [sys.executable, "-m", "playwright", "install"]
    if with_deps:
        args.append("--with-deps")
    args.append("chromium")
    run_command(args)


def install_camoufox() -> None:
    run_command([sys.executable, "-m", "camoufox", "fetch"])


def expected_geoip_paths(config: dict) -> Iterable[Path]:
    from camoufox.geolocation import get_mmdb_path

    urls = config.get("urls", {})
    if "combined" in urls:
        yield get_mmdb_path("combined", config)
        return

    for ip_version in urls:
        yield get_mmdb_path(str(ip_version), config)


def install_geoip(source: str | None) -> None:
    from camoufox.geolocation import (
        _get_geoip_config_by_name,
        download_mmdb,
        load_geoip_config,
        needs_update,
    )

    config = _get_geoip_config_by_name(source) if source else load_geoip_config()
    paths = list(expected_geoip_paths(config))
    missing = [path for path in paths if not path.exists()]

    if missing or needs_update(config):
        label = config.get("name", "GeoIP")
        print(f"Downloading {label} database...", flush=True)
        download_mmdb(source)
    else:
        print(f"GeoIP database is up to date: {paths[0].parent}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download Playwright, Camoufox, and GeoIP runtime assets."
    )
    parser.add_argument(
        "--skip-playwright",
        action="store_true",
        help="Do not install the Playwright Chromium browser.",
    )
    parser.add_argument(
        "--playwright-with-deps",
        action="store_true",
        help="Pass --with-deps to playwright install, mainly for Linux/Docker.",
    )
    parser.add_argument(
        "--skip-camoufox",
        action="store_true",
        help="Do not install the Camoufox browser binary.",
    )
    parser.add_argument(
        "--skip-geoip",
        action="store_true",
        help="Do not download the Camoufox GeoIP database.",
    )
    parser.add_argument(
        "--geoip-source",
        default=None,
        help="Optional Camoufox GeoIP source name, for example 'MaxMind GeoLite2'.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.skip_playwright:
        install_playwright(with_deps=bool(args.playwright_with_deps))
    if not args.skip_camoufox:
        install_camoufox()
    if not args.skip_geoip:
        install_geoip(source=args.geoip_source)

    print("Browser runtime assets are ready.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
