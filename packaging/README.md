# Building Qualm.app

    uv run python packaging/build_app.py

This writes `dist/Qualm.app` (about 150 MB) and `dist/Qualm.dmg` (about 90 MB). It needs a Mac with Apple
silicon, uv with a managed CPython 3.12 (`uv python install 3.12`), and the Command Line Tools (clang). The
script picks the newest macOS SDK that the installed linker can actually link against. The version is
pyproject.toml's; the build number (CFBundleVersion, which Sparkle compares) is the number of commits, so
it needs the whole history (or `QUALM_BUILD_NUMBER`).

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
- `Contents/Frameworks/Sparkle.framework`: the updater ([Sparkle](https://sparkle-project.org) 2.10.0,
  pinned by version and SHA-256, downloaded once into `dist/.cache`). `src/qualm/updates.py` starts it; the
  keys in Info.plist say where the feed is (`appcast.xml` on the latest GitHub release), the public half of
  the signing key, a daily check, a signed feed, an update checked before it's unpacked, and never an
  install without asking.

## Signing

The app is **ad-hoc signed and not notarized**:

- On the Mac that built it, it opens normally, because a local build isn't quarantined.
- On any other Mac, macOS blocks the first open. Try to open it once, then go to System Settings → Privacy &
  Security and click Open Anyway. (Right-click → Open got past this before macOS 15; it no longer does.)
- **Every rebuild, and so every update, changes the ad-hoc signature**, so macOS treats it as a new app. The old Accessibility
  entry still shows as on but no longer applies. Remove "Qualm" from System Settings → Privacy & Security →
  Accessibility (the − button), then add the new one or accept the prompt. Screen Recording works the same
  way.

## Releases

A release is a tag. Bump `version` in pyproject.toml (PEP 440: `0.1.0b2`, `0.1.0`), write its section in
[docs/CHANGELOG.md](../docs/CHANGELOG.md) (`## 0.1.0b2`: it becomes the release notes, on GitHub and in the
update window), commit, then:

    git tag v0.1.0b2 && git push origin v0.1.0b2

The Release workflow ([.github/workflows/release.yml](../.github/workflows/release.yml)) runs the tests,
builds the app on an Apple silicon runner, signs the DMG and the feed, and publishes the GitHub release with
`Qualm.dmg` and `appcast.xml` (`packaging/release.py --publish`), about 10 minutes. Qualm.app everywhere
finds it within a day, or at once with *Check for Updates…*. The tag must be `v` + the version, or the
workflow stops. Run by hand (Actions > Release > Run workflow), it builds and signs without publishing.

By hand, the same thing: `uv run python packaging/release.py` writes `dist/Qualm.dmg`, `dist/appcast.xml` and
`dist/release-notes.md`; `--publish` also creates the release (with `gh`, for a tag already pushed).

- **Every release is marked latest, betas too.** The feed is `releases/latest/download/appcast.xml`: a
  GitHub pre-release is never "latest", so it would reach nobody. A beta says so in its version.
- **The signing key.** Updates and the feed are signed with an EdDSA key made by Sparkle's `generate_keys`.
  Its private half is in the maintainer's login keychain (Keychain Access: *Private key for signing Sparkle
  updates*, account `ed25519`) and in the repository secret `SPARKLE_ED_PRIVATE_KEY`; the public half is
  `SPARKLE_PUBLIC_KEY` in build_app.py, inside every copy of the app. **Keep a backup** (`generate_keys -x
  FILE` exports it): without it, no copy already out there will accept another update, and everyone
  would have to download the next version by hand. Never commit it.
- **Build numbers only go up.** They count the commits on the tagged branch, so don't rewrite history
  that has been released.
- **After an update, Accessibility again.** Until the app is signed with a Developer ID, each version is a
  new app to macOS; Qualm says how when it opens. Updates are never installed silently for that reason
  (`SUAllowsAutomaticUpdates` is off). With a Developer ID and notarization, both go away: sign with it in
  build_app.py (hardened runtime), notarize the DMG in the workflow, and keep the same EdDSA key.
