#!/usr/bin/env python3
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURES = {"arm64", "x86_64"}


def run(command: list[str], env: dict[str, str]) -> None:
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def main() -> None:
    if sys.platform != "darwin":
        raise SystemExit("macOS application builds must run on macOS.")
    if len(sys.argv) != 2 or not re.fullmatch(r"v\d+\.\d+\.\d+", sys.argv[1]):
        raise SystemExit("Usage: python scripts/build_macos.py vMAJOR.MINOR.PATCH")

    architecture = platform.machine()
    if architecture not in ARCHITECTURES:
        raise SystemExit(f"Unsupported Mac architecture: {architecture}")

    build_dir = ROOT / "build" / "macos" / architecture
    browser_dir = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", build_dir / "ms-playwright"))
    dist_dir = ROOT / "dist" / "macos" / architecture
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(browser_dir)}
    browser_dir.mkdir(parents=True, exist_ok=True)
    dist_dir.mkdir(parents=True, exist_ok=True)

    run([sys.executable, "-m", "playwright", "install", "chromium"], env)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        "auto-chope-seat",
        "--osx-bundle-identifier",
        "com.icalaut.auto-chope-seat",
        "--specpath",
        str(build_dir),
        "--collect-all",
        "playwright",
        "--collect-all",
        "airportsdata",
        "--add-data",
        f"{ROOT / 'stations.json'}:.",
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(build_dir / "pyinstaller"),
        str(ROOT / "app.py"),
    ]
    signing_identity = os.environ.get("PYINSTALLER_CODESIGN_IDENTITY")
    if signing_identity:
        command[command.index("--distpath"):command.index("--distpath")] = [
            "--codesign-identity",
            signing_identity,
        ]
    run(command, env)

    app_bundle = dist_dir / "auto-chope-seat.app"
    info_plist = app_bundle / "Contents" / "Info.plist"
    resources = app_bundle / "Contents" / "Resources"
    resources.mkdir(parents=True, exist_ok=True)
    shutil.copytree(browser_dir, resources / "ms-playwright", dirs_exist_ok=True)
    version = sys.argv[1][1:]
    run(["/usr/libexec/PlistBuddy", "-c",
         f"Set :CFBundleShortVersionString {version}", str(info_plist)], env)
    run(["/usr/libexec/PlistBuddy", "-c",
         f"Add :CFBundleVersion string {version}", str(info_plist)], env)
    print(f"Built {app_bundle} for {architecture} ({version})")


if __name__ == "__main__":
    main()
