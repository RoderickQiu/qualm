---
name: qualm
description: Set up, change or check Qualm's rules for the user - what it blocks or time-limits, when, exceptions, apps and sites it leaves alone, settings. Use when the user asks to block, limit, allow, pause-by-schedule or stop flagging something, asks why Qualm popped up, or asks what their rules are. Drives the `qualm` CLI; never edits rules.toml by hand.
---

# Driving Qualm for the user

Everything goes through `uv run qualm …` in the qualm repo.
Always pass `--json` and read the result; errors come back as
`{"error": {"code", "message"}}` with exit codes 2 invalid, 3 not found,
4 over the question limit, 5 app or model down. Don't edit rules.toml
directly: the commands check every change (a broken or over-limit file is
never saved) and keep a backup.

## Start here

```bash
uv run qualm status --json        # app running? model up? config ok? question budget
uv run qualm rules list --json    # {"capacity": {...}, "rules": [...]}, each with a one-line summary
uv run qualm schema --json        # every field: type, default, meaning; the grammar of sites and when
```

## The question limit

Every reading asks the model one question per rule that's on at that moment,
one per allow class, and 3 shared ones. Past ~25 the local model slows
sharply (13 questions 0.6 s, 28 1.8 s, 53 up to 18 s, 103 timed out and
swapped the Mac), so `settings.max_questions` (25) is enforced over the
whole week. `capacity.peak` in any output says how full it is. When a change
is refused with code `over_limit`, tell the user and offer, in this order:

1. give rules hours that don't overlap (`when`): a work-hours rule and an
   evening rule share one slot;
2. fold two related rules into one description (keep the one with more
   history: its id holds the learned exceptions and answers);
3. switch rarely-hit rules off (`rules off`; `rules list` shows today's usage).

Don't raise `max_questions` unless the user asks and understands it slows
every reading.

## Changing things

- One change: `rules add|set|on|off|remove`, `allow …`, `except …`, `never …`,
  `settings set`. Syntax: `key=value`, `key+=item`, `key-=item`, `key=` to
  remove; `what="…"` sets the text the model reads.
- Several changes, or anything near the limit: `config export` → edit the
  JSON → `config apply FILE --dry-run --json` (read `diff`) → `config apply
  FILE --json`. All of it is checked together and saved together, or not at all.
  Entries left out of the JSON are kept; only `--prune` removes them, so
  use it only after the user agreed to what the dry run's diff removes.
- Any change: add `--dry-run` first when unsure; `config undo` reverts the
  last saved change.

Map the user's words onto fields, e.g. "no streams during work, lectures
are fine" → `rules set livestream 'when+=mon-fri 09:00-18:00' allow_learning=true`.
Describe modes, not sites: "short videos made for endless swiping", with
sites (`douyin.com`, `youtube.com/shorts`) only for what must always count.

## A new rule: test before trusting it

A new rule's threshold (0.2) is a guess. Before switching it on:

```bash
uv run qualm rules test NAME --what "…" --json     # a draft: nothing saved
uv run qualm rules add NAME --what "…" --off --json
uv run qualm rules test NAME --json                # rows: id, p_hit, does, title, url
uv run qualm rules label NAME --yes ID… --no ID… --json
uv run qualm rules tune NAME --apply --json        # needs >= 3 yes and 3 no
uv run qualm rules on NAME --json
```

Judge rows from their title and URL; when unsure, show the user the top
rows and ask. If the right pages don't score above the wrong ones, change
the wording (`--what`) and test again rather than forcing a threshold.
`rules test` needs the model server (`status`) and takes ~0.3 s a screen.

## Don't

- start, stop or restart the app or the model server without asking;
- remove rules, exceptions or never-here places the user didn't mention;
- use `--data` / `--rules` paths other than the defaults unless told to.
