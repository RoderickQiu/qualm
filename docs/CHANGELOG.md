# Changes

Each version's section is its release notes: the release workflow publishes the section headed with
the version, on GitHub and in the app's update window.

## Unreleased

Rename this heading to the next version when it's released.

- **About Qualm** in the menu bar: the version, who made Qualm, and a button to the website.
- **Claude Code knows Qualm.** Setup gives Claude Code Qualm's skill (`~/.claude/skills/qualm`),
  so any session can change your rules, say why Qualm popped up, or find out why it isn't
  working, without a prompt pasted first. *Qualm skill for Claude Code* in the menu, or
  `qualm skill install|remove`, adds or removes it; Qualm keeps it current when it updates, and
  leaves a skill of that name it didn't write alone.
- **`qualm logs`** says where the logs are and prints the app's last lines (`--model` the local
  model's); `qualm status --json` has the paths too. Qualm.app opened from Finder now writes its
  log as well, not only when it starts at login.

## 0.1.0b1

The first beta, for people who already know what Kev and Jev are.

- **Qualm reads your screen and steps in** when you drift into short video, feeds or livestreams,
  and stays out of the way when you're learning or working. Rules are sentences, not site lists.
- **Local-first.** Kev-4B runs on your Mac (Apple silicon, macOS 14, 6–7 GB of memory free); the
  first start downloads about 6 GB. Hosted Jev is a switch in setup, never a fallback.
- **Check-ins, not daily budgets**, focus sessions, and a dashboard with no score and no streak.
- **Rules change through your AI agent** (Claude Code, Codex, Cursor): `qualm guide`.
- **Updates.** Qualm checks GitHub once a day and says in the menu bar when a new version is out;
  it installs only when you say so. *Check for Updates…* in the menu asks now, and *Check for
  updates automatically* turns the daily check off. Nothing about you or your screen is sent.

Known limits: the app isn't notarized (the first open needs *Open Anyway* in System Settings >
Privacy & Security), and after each update macOS asks for Accessibility again. Apple silicon only,
English interface. A live coding stream on Twitch still gets a pop-up.
