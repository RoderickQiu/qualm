# Building Qualm.app

    uv run python packaging/build_app.py

This writes `dist/Qualm.app` (about 150 MB) and `dist/Qualm.dmg` (about 90 MB). It needs a Mac with Apple
silicon, uv with a managed CPython 3.12 (`uv python install 3.12`), and the Command Line Tools (clang). The
script picks the newest macOS SDK that the installed linker can actually link against.

A build is a copy of the code as it was when you built it, and `dist/` is git-ignored, so updating the
checkout leaves an old build in place. Rebuild after every update before you open it (quit the running
Qualm first): an old build runs its old code, and one from before the audit of September 2026 has no
one-copy lock and an unpinned model that can start a 16 GB local build.

The app is arm64 only (the launcher, Python and uv inside it), so it runs on Apple silicon and not on an
Intel Mac. Users should drag it to Applications and open it from there: setup won't make a login item or a
`qualm` command from the disk image, or from a copy macOS moved aside to open it.

What's inside:

- `Contents/MacOS/Qualm`: `launcher.c`, which embeds the bundled Python, so the running process is Qualm.app
  itself. macOS asks for Accessibility, Automation and Screen Recording for "Qualm", not for python3, and menu
  bar managers list it as Qualm. With no arguments it runs `qualm app`; with arguments it behaves like
  `python`, so `Qualm -m qualm doctor` works, and so does a shim that runs `exec …/Qualm -m qualm "$@"`.
- `Contents/Resources/python`: the uv-managed CPython 3.12 without tests, IDLE or Tk. Qualm and its own
  dependencies are installed into it (not editable), and all bytecode is compiled before signing. The launcher
  never writes `.pyc`, because a new file inside the bundle would break its signature.
- `Contents/Resources/bin/uv`: installs the local model's runtime (Kev, PyTorch, MLX) on demand into
  `~/Library/Application Support/Qualm`. None of that is in the app.

## Signing

The app is **ad-hoc signed and not notarized**:

- On the Mac that built it, it opens normally, because a local build isn't quarantined.
- On any other Mac, macOS blocks the first open. Try to open it once, then go to System Settings → Privacy &
  Security and click Open Anyway. (Right-click → Open got past this before macOS 15; it no longer does.)
- **Every rebuild changes the ad-hoc signature**, so macOS treats it as a new app. The old Accessibility
  entry still shows as on but no longer applies. Remove "Qualm" from System Settings → Privacy & Security →
  Accessibility (the − button), then add the new one or accept the prompt. Screen Recording works the same
  way.
