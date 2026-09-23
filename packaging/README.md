# Building Qualm.app

    uv run python packaging/build_app.py

This writes `dist/Qualm.app` (about 150 MB) and `dist/Qualm.dmg` (about 90 MB). It needs a Mac with Apple
silicon, uv with a managed CPython 3.12 (`uv python install 3.12`), and the Command Line Tools (clang). The
script picks the newest macOS SDK that the installed linker can actually link against.

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
- On any other Mac, macOS blocks the first open. Either right-click Qualm.app and choose Open, or try to open
  it once, then go to System Settings → Privacy & Security and click Open Anyway.
- **Every rebuild changes the ad-hoc signature**, so macOS treats it as a new app. The old Accessibility
  entry still shows as on but no longer applies. Remove "Qualm" from System Settings → Privacy & Security →
  Accessibility (the − button), then add the new one or accept the prompt. Screen Recording works the same
  way.
