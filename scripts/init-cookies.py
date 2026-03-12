#!/usr/bin/env python3
"""
Cookie initialization script for douyin-downloader.

This script runs on the HOST machine (not in Docker) to:
1. Launch a browser window for Douyin login
2. Capture cookies after successful login
3. Update config.yml with the captured cookies

Usage:
    # First time setup (requires pip install)
    pip install playwright pyyaml
    playwright install chromium
    
    # Run the script
    python scripts/init-cookies.py
    
    # Or with custom config path
    python scripts/init-cookies.py --config /path/to/config.yml
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add app directory to path for imports
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
APP_DIR = PROJECT_ROOT / "app"

sys.path.insert(0, str(APP_DIR))


def parse_args():
    parser = argparse.ArgumentParser(
        description="Initialize Douyin cookies by launching a browser for login."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "config.yml",
        help="Path to config.yml file (default: ./config.yml)",
    )
    parser.add_argument(
        "--browser",
        choices=["chromium", "firefox", "webkit"],
        default="chromium",
        help="Browser engine to use (default: chromium)",
    )
    return parser.parse_args()


async def main():
    args = parse_args()
    
    # Check if config exists
    if not args.config.exists():
        example_config = PROJECT_ROOT / "config.example.yml"
        if example_config.exists():
            print(f"[INFO] Config file not found at {args.config}")
            print(f"[INFO] Copying from {example_config}...")
            import shutil
            shutil.copy(example_config, args.config)
            print(f"[INFO] Created {args.config}")
        else:
            print(f"[ERROR] Config file not found: {args.config}")
            print("[ERROR] Please create config.yml from config.example.yml first.")
            return 1
    
    # Check Playwright installation
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("[ERROR] Playwright is not installed.")
        print("[INFO] Please run:")
        print("       pip install playwright")
        print("       playwright install chromium")
        return 1
    
    # Import the cookie fetcher from app
    try:
        from tools.cookie_fetcher import capture_cookies, parse_args as fetcher_parse_args
    except ImportError as e:
        print(f"[ERROR] Failed to import cookie_fetcher: {e}")
        print("[INFO] Make sure you're running from the project root directory.")
        return 1
    
    print("=" * 60)
    print("  Douyin Cookie Initialization")
    print("=" * 60)
    print()
    print("This script will:")
    print("  1. Open a browser window")
    print("  2. Navigate to Douyin")
    print("  3. Wait for you to log in (scan QR code or enter credentials)")
    print("  4. Capture cookies and save to config.yml")
    print()
    print("Press Enter to start...")
    input()
    
    # Prepare arguments for cookie_fetcher
    fetcher_args = fetcher_parse_args([
        "--browser", args.browser,
        "--config", str(args.config),
    ])
    
    print()
    print("[INFO] Launching browser...")
    print("[INFO] Please log in to Douyin in the browser window.")
    print("[INFO] After logging in, press Enter in THIS terminal to continue.")
    print()
    
    result = await capture_cookies(fetcher_args)
    
    if result == 0:
        print()
        print("=" * 60)
        print("  ✅ Cookies captured successfully!")
        print("=" * 60)
        print()
        print(f"Config updated: {args.config}")
        print()
        print("You can now start the Docker container:")
        print("  docker compose up -d --build")
        print()
    else:
        print()
        print("=" * 60)
        print("  ❌ Failed to capture cookies")
        print("=" * 60)
        print()
        print("Please try again or manually copy cookies from browser DevTools.")
        print()
    
    return result


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n[INFO] Cancelled by user.")
        sys.exit(1)
