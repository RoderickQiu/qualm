"""Build dist/Qualm.app and dist/Qualm.dmg: self-contained, ad-hoc signed, not notarized.

    uv run python packaging/build_app.py

The app carries its own Python (the uv-managed CPython 3.12, pruned), Qualm and
its own dependencies installed into it, uv (to install the local model's
runtime on demand, into ~/Library/Application Support/Qualm), and a small
launcher (launcher.c) that embeds that Python, so the running process is
Qualm.app itself. The local model (Kev, PyTorch, MLX) is not in the bundle.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DIST = REPO / "dist"
APP = DIST / "Qualm.app"
PY_VERSION = "3.12"
BUNDLE_ID = "com.qualm.app"
# Pruned from the bundled Python: tests, the IDE and Tk (nothing in Qualm uses them).
PRUNE_DIRS = {"test", "tests", "idlelib", "tkinter", "turtledemo", "ensurepip"}
PRUNE_LIB = ("tcl9", "tcl9.0", "tk9.0", "itcl4.3.5", "thread3.0.4", "libtcl9.0.dylib", "libtcl9tk9.0.dylib")


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


def info_plist(version: str) -> dict:
    return {
        "CFBundleName": "Qualm",
        "CFBundleDisplayName": "Qualm",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": "Qualm",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "CFBundleIconFile": "Qualm",
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSUIElement": True,  # a menu bar app: no Dock icon
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSAppleEventsUsageDescription": "Qualm asks your browser to go back and reads the address of the front tab.",
        "NSHumanReadableCopyright": "Apache-2.0",
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
        plistlib.dump(info_plist(version), f)
    (contents / "PkgInfo").write_text("APPL????")

    # Every .pyc written now, before signing: the launcher never writes bytecode,
    # and a .pyc added later would break the bundle's seal.
    python = contents / "Resources" / "python" / "bin" / f"python{PY_VERSION}"
    run(python, "-m", "compileall", "-q", "-j", "0", contents / "Resources" / "python" / "lib" / f"python{PY_VERSION}",
        stdout=subprocess.DEVNULL)
    run("codesign", "--force", "--deep", "--sign", "-", "--identifier", BUNDLE_ID, APP)
    run("codesign", "--verify", "--deep", "--strict", "--verbose=2", APP)
    dmg = build_dmg()
    print(f"\nbuilt {APP}\n      {dmg}\nAd-hoc signed, not notarized: see packaging/README.md.")


if __name__ == "__main__":
    main()
