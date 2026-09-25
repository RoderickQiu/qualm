"""Build dist/Qualm.app and dist/Qualm.dmg: self-contained, ad-hoc signed, not notarized.

    uv run python packaging/build_app.py

The app carries its own Python (the uv-managed CPython 3.12, pruned), Qualm and
its own dependencies installed into it, uv (to install the local model's
runtime on demand, into ~/Library/Application Support/Qualm), Sparkle (the
updater: src/qualm/updates.py), and a small launcher (launcher.c) that embeds
that Python, so the running process is Qualm.app itself. The local model (Kev,
PyTorch, MLX) is not in the bundle.

The version is pyproject.toml's; the build number (CFBundleVersion, which
Sparkle compares) is the commit count, or QUALM_BUILD_NUMBER.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DIST = REPO / "dist"
APP = DIST / "Qualm.app"
PY_VERSION = "3.12"
BUNDLE_ID = "com.qualm.app"
# Pruned from the bundled Python: tests, the IDE and Tk (nothing in Qualm uses them).
PRUNE_DIRS = {"test", "tests", "idlelib", "tkinter", "turtledemo", "ensurepip"}
PRUNE_LIB = ("tcl9", "tcl9.0", "tk9.0", "itcl4.3.5", "thread3.0.4", "libtcl9.0.dylib", "libtcl9tk9.0.dylib")
# Sparkle, pinned: the updater Qualm.app embeds, and the tools a release signs with (packaging/release.py).
SPARKLE_VERSION = "2.10.0"
SPARKLE_SHA256 = "c2bf58aa8387266ac179357b1415d6f2635f044da8be41042af32425dae6da0c"
SPARKLE_URL = (f"https://github.com/sparkle-project/Sparkle/releases/download/{SPARKLE_VERSION}/"
               f"Sparkle-{SPARKLE_VERSION}.tar.xz")
# The public half of the EdDSA key every update and feed is signed with. The private half is in the
# maintainer's login keychain and in the repository's SPARKLE_ED_PRIVATE_KEY secret (packaging/README.md).
SPARKLE_PUBLIC_KEY = "PqdDdEg6RuD/orHWVaBILqn1BdIS/Q0RbUz/KgzfSWY="
CACHE = DIST / ".cache"


def run(*cmd, **kw) -> subprocess.CompletedProcess:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def python_root() -> Path:
    """The uv-managed CPython install dir (not a venv)."""
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    exe = subprocess.run(["uv", "python", "find", "--system", PY_VERSION], capture_output=True, text=True, env=env)
    if exe.returncode != 0 or not exe.stdout.strip():
        exe = subprocess.run(["uv", "python", "find", PY_VERSION], capture_output=True, text=True, check=True, env=env)
    root = Path(exe.stdout.strip()).resolve().parents[1]
    if not (root / "lib" / f"libpython{PY_VERSION}.dylib").exists():
        sys.exit(f"{root} has no libpython{PY_VERSION}.dylib: need a uv-managed CPython (uv python install {PY_VERSION})")
    return root


def copy_python(src: Path, dst: Path) -> None:
    def ignore(folder, names):
        skip = {n for n in names if n in PRUNE_DIRS}
        if Path(folder) == src / "lib":
            skip |= {n for n in names if n in PRUNE_LIB}
        return skip

    shutil.copytree(src, dst, symlinks=True, ignore=ignore)
    (dst / "lib" / f"python{PY_VERSION}" / "EXTERNALLY-MANAGED").unlink(missing_ok=True)
    # Tk's extension module would fail to load without its libraries.
    for so in (dst / "lib" / f"python{PY_VERSION}" / "lib-dynload").glob("_tkinter*"):
        so.unlink()


def sparkle() -> Path:
    """Sparkle's release, unpacked in dist/.cache: Sparkle.framework and bin/ (sign_update). Checked against
    its pinned SHA-256 before it's used."""
    dest = CACHE / f"Sparkle-{SPARKLE_VERSION}"
    if (dest / "Sparkle.framework").exists() and (dest / "bin" / "sign_update").exists():
        return dest
    archive = CACHE / f"Sparkle-{SPARKLE_VERSION}.tar.xz"
    CACHE.mkdir(parents=True, exist_ok=True)
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != SPARKLE_SHA256:
        print(f"+ download {SPARKLE_URL}", flush=True)
        with urllib.request.urlopen(SPARKLE_URL, timeout=300) as r:
            archive.write_bytes(r.read())
    got = hashlib.sha256(archive.read_bytes()).hexdigest()
    if got != SPARKLE_SHA256:
        sys.exit(f"{archive}: SHA-256 {got}, expected {SPARKLE_SHA256}")
    tmp = CACHE / f".unpack-{os.getpid()}"
    tmp.mkdir()
    run("tar", "-xf", archive, "-C", tmp)
    tmp.rename(dest)
    return dest


def add_sparkle(frameworks: Path) -> None:
    """Sparkle.framework into Contents/Frameworks, ad-hoc signed from the inside out with its own
    identifiers (a --deep signature of the app would give its helpers Qualm's)."""
    fw = frameworks / "Sparkle.framework"
    frameworks.mkdir(parents=True, exist_ok=True)
    shutil.copytree(sparkle() / "Sparkle.framework", fw, symlinks=True)
    b = fw / "Versions" / "B"
    for part in (b / "XPCServices" / "Installer.xpc", b / "XPCServices" / "Downloader.xpc", b / "Autoupdate",
                 b / "Updater.app", fw):
        extra = ["--preserve-metadata=entitlements"] if part.name == "Downloader.xpc" else []
        run("codesign", "--force", "--sign", "-", *extra, part)


def build_number() -> int:
    """CFBundleVersion: QUALM_BUILD_NUMBER, else the number of commits (a full clone: CI fetches all history)."""
    if n := os.environ.get("QUALM_BUILD_NUMBER"):
        return int(n)
    out = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=REPO, capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip().isdigit():
        sys.exit("no build number: not a git checkout? set QUALM_BUILD_NUMBER")
    if subprocess.run(["git", "rev-parse", "--is-shallow-repository"], cwd=REPO, capture_output=True,
                      text=True).stdout.strip() == "true":
        sys.exit("a shallow clone counts too few commits: fetch the whole history, or set QUALM_BUILD_NUMBER")
    return int(out.stdout.strip())


def install_qualm(python: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    # Non-editable, so package data (rules.example.toml, dashboard.html, ...) is copied in.
    run("uv", "pip", "install", "--python", python, "--break-system-packages", "--no-cache", "--compile-bytecode",
        REPO, env=env)


def pick_sdk() -> list[str]:
    """-isysroot for the first SDK this linker can link against: the default
    one can be newer than ld (Command Line Tools with a macOS 27 SDK and an
    older ld fail with "tapi error: unknown architecture")."""
    default = subprocess.run(["xcrun", "--show-sdk-path"], capture_output=True, text=True).stdout.strip()
    dirs = [Path("/Library/Developer/CommandLineTools/SDKs"),
            Path("/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs")]
    found = sorted({p.resolve() for d in dirs if d.exists() for p in d.glob("MacOSX*.sdk")}, reverse=True)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "t.c"
        src.write_text("int main(void) { return 0; }\n")
        for sdk in [Path(default)] * bool(default) + found:
            ok = subprocess.run(["clang", "-isysroot", str(sdk), str(src), "-o", str(Path(tmp) / "t")],
                                capture_output=True).returncode == 0
            if ok:
                print(f"SDK: {sdk}", flush=True)
                return ["-isysroot", str(sdk)]
    sys.exit("no macOS SDK links with this toolchain: install or update the Command Line Tools")


def build_launcher(py_root: Path, out: Path) -> None:
    inc = py_root / "include" / f"python{PY_VERSION}"
    lib = py_root / "lib"
    run("clang", "-O2", "-mmacosx-version-min=13.0", *pick_sdk(), f"-I{inc}", REPO / "packaging" / "launcher.c",
        f"-L{lib}", f"-lpython{PY_VERSION}", "-o", out)
    # libpython's install name is an absolute path into the build machine's uv cache.
    old = subprocess.run(["otool", "-D", lib / f"libpython{PY_VERSION}.dylib"], capture_output=True, text=True,
                         check=True).stdout.strip().splitlines()[-1]
    new = f"@executable_path/../Resources/python/lib/libpython{PY_VERSION}.dylib"
    run("install_name_tool", "-change", old, new, out)
    linked = subprocess.run(["otool", "-L", out], capture_output=True, text=True, check=True).stdout
    if new not in linked:
        sys.exit(f"launcher still links {old}:\n{linked}")


def build_icon(out: Path) -> None:
    """docs/assets/logo.svg on the macOS icon grid: the rounded square at
    824 of 1024 px, centred, transparent around it."""
    from AppKit import (NSBitmapImageRep, NSCalibratedRGBColorSpace, NSGraphicsContext, NSImage, NSMakeRect,
                        NSPNGFileType)

    logo = NSImage.alloc().initWithContentsOfFile_(str(REPO / "docs" / "assets" / "logo.svg"))
    if logo is None:
        sys.exit("couldn't read docs/assets/logo.svg")
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "Qualm.iconset"
        iconset.mkdir()
        for base in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = base * scale
                rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
                    None, px, px, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0)
                ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
                NSGraphicsContext.saveGraphicsState()
                NSGraphicsContext.setCurrentContext_(ctx)
                inner = px * 824 / 1024
                off = (px - inner) / 2
                logo.drawInRect_(NSMakeRect(off, off, inner, inner))
                ctx.flushGraphics()
                NSGraphicsContext.restoreGraphicsState()
                name = f"icon_{base}x{base}{'@2x' if scale == 2 else ''}.png"
                rep.representationUsingType_properties_(NSPNGFileType, {}).writeToFile_atomically_(
                    str(iconset / name), True)
        shutil.copyfile(iconset / "icon_512x512@2x.png", DIST / "icon-1024.png")  # to look at
        run("iconutil", "-c", "icns", iconset, "-o", out)


def info_plist(version: str, build: int) -> dict:
    from qualm.updates import CHECK_EVERY_S, FEED

    return {
        "CFBundleName": "Qualm",
        "CFBundleDisplayName": "Qualm",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": "Qualm",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": str(build),
        "CFBundleIconFile": "Qualm",
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSUIElement": True,  # a menu bar app: no Dock icon
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSAppleEventsUsageDescription": "Qualm asks your browser to go back and reads the address of the front tab.",
        "NSHumanReadableCopyright": "© 2026 Tianrun Qiu. Free software under the GNU GPL v3 or later.",
        # Sparkle (updates.py): a daily check, disclosed in the README; installing always asks (an ad-hoc signed
        # app needs Accessibility again after each update, so never silently); the feed and each update must be
        # signed with the key above, and an update is checked before it's unpacked.
        "SUFeedURL": FEED,
        "SUPublicEDKey": SPARKLE_PUBLIC_KEY,
        "SUEnableAutomaticChecks": True,
        "SUScheduledCheckInterval": CHECK_EVERY_S,
        "SUAllowsAutomaticUpdates": False,
        "SURequireSignedFeed": True,
        "SUVerifyUpdateBeforeExtraction": True,
    }


def build_dmg() -> Path:
    dmg = DIST / "Qualm.dmg"
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "Qualm"
        stage.mkdir()
        shutil.copytree(APP, stage / "Qualm.app", symlinks=True)
        (stage / "Applications").symlink_to("/Applications")
        run("hdiutil", "create", "-volname", "Qualm", "-srcfolder", stage, "-ov", "-format", "UDZO", dmg)
    return dmg


def main() -> None:
    version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    build = build_number()
    py_root = python_root()
    DIST.mkdir(exist_ok=True)
    if APP.exists():
        shutil.rmtree(APP)
    contents = APP / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Resources" / "bin").mkdir(parents=True)

    print(f"Python: {py_root}", flush=True)
    copy_python(py_root, contents / "Resources" / "python")
    install_qualm(contents / "Resources" / "python" / "bin" / f"python{PY_VERSION}")

    uv = shutil.which("uv")
    if not uv:
        sys.exit("uv not found on PATH")
    shutil.copy2(Path(uv).resolve(), contents / "Resources" / "bin" / "uv")

    build_launcher(py_root, contents / "MacOS" / "Qualm")
    build_icon(contents / "Resources" / "Qualm.icns")
    with (contents / "Info.plist").open("wb") as f:
        plistlib.dump(info_plist(version, build), f)
    (contents / "PkgInfo").write_text("APPL????")

    # Every .pyc written now, before signing: the launcher never writes bytecode,
    # and a .pyc added later would break the bundle's seal.
    python = contents / "Resources" / "python" / "bin" / f"python{PY_VERSION}"
    run(python, "-m", "compileall", "-q", "-j", "0", contents / "Resources" / "python" / "lib" / f"python{PY_VERSION}",
        stdout=subprocess.DEVNULL)
    add_sparkle(contents / "Frameworks")
    run("codesign", "--force", "--sign", "-", "--identifier", BUNDLE_ID, APP)
    run("codesign", "--verify", "--deep", "--strict", "--verbose=2", APP)
    dmg = build_dmg()
    print(f"\nbuilt {APP} {version} (build {build})\n      {dmg}\nAd-hoc signed, not notarized: see packaging/README.md.")


if __name__ == "__main__":
    main()
