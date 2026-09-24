# Driving Qualm for the user

Everything goes through the `qualm` command: `qualm …` where Qualm.app
added it (~/.local/bin/qualm), `uv run qualm …` in a checkout of the repo.
`qualm guide` prints this. The rules and data live in
`~/Library/Application Support/Qualm`, whichever folder you run it from;
`qualm setup --backend kev|jev --rules a,b --no-login` sets it up without
questions, and `qualm settings set backend=jev` switches the model.
Always pass `--json` and read the result; errors come back as
`{"error": {"code", "message"}}` with exit codes 2 invalid, 3 not found,
4 over the question limit, 5 app or model down. Don't edit rules.toml
directly: the commands check every change (a broken or over-limit file is
never saved) and keep a backup.

## Measure, don't guess

Rules and exceptions are sentences a small model reads, and it reacts to
words, not intentions. Wording that reads right can make things worse: an
exception naming the apps to leave alone ("chatting in WeChat, WhatsApp or
iMessage is fine") pushed WhatsApp's score on the social rule from ~0.17 to
~0.50. So never save a wording you haven't scored on the user's own screens
(`rules test`, `allow test`), and report the numbers when you're done: what
scored what, before and after.

## Start here

```bash
qualm status --json        # app running? model up? config ok? question budget
qualm rules list --json    # {"capacity": {...}, "rules": [...]}, each with a one-line summary
qualm schema --json        # every field: type, default, meaning; the grammar of sites and when
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

Two kinds: `deny` steps in at once; `check_in` asks what for and for how
long (5/15/30 min) on arrival and steps in when that time is up. "Limit my
YouTube to 30 minutes a day" → a `check_in` rule: there are no daily
budgets (docs/POLICY.md says why); say so, and offer `max_wait_s` or
`extensions=0` if they want it firmer. An old rules.toml with `time_cap`
or `budgets` won't load: `qualm config migrate --dry-run --json`, then
without `--dry-run`.

## Something pops up where it shouldn't

1. Find it: `qualm rules test RULE --last 300 --json` lists recent screens
   with their scores (`does`, `app`, `title`, `url`). Note where the wrong
   ones sit against the threshold, and where the right hits sit.
2. One place (a window, a page): the pop-up's *Not this one* already
   handles it. On a page with an address it lets that address through; in
   an app window it lets that window through below the score it had plus
   0.1 (`except list` shows these). *Never here* silences a whole app or site.
3. A kind of page (chats, a store, a player): an allow class, not an
   exception. Draft it and score it before saving:
   `qualm allow test chat --what "a chat conversation in a messaging app:
   messages between people, in a private or group chat" --last 500 --json`.
   Keep it only if the pages it should cover score above its threshold,
   the pages the user wants flagged score well below, and `would_stop`
   names only wrong pop-ups. Then `allow add ID --what "…" --threshold T`.
   Each allow class is one more question per reading (see the limit below).
4. A rule that's too broad or too narrow everywhere: new wording through
   `rules test NAME --what "…"`, then `label` and `tune`, as below.

Don't paste app or site names into exception text or a rule's wording to
exclude them: see "Measure, don't guess".

## Focus and pause

"I need to write the report for an hour, keep me on it" →
`qualm focus write the report --minutes 60 --json`: until it ends,
every rule hit steps in at once (check-in rules too) and the pop-up names the
intent. `qualm focus --json` shows it, `--stop` ends it. "Leave me alone
for 20 minutes" → `qualm pause 20 --json`; `pause --stop` resumes.
These don't touch rules.toml and need no dry run.

## A new rule: test before trusting it

A new rule's threshold (0.2) is a guess. Before switching it on:

```bash
qualm rules test NAME --what "…" --json     # a draft: nothing saved
qualm rules add NAME --what "…" --off --json
qualm rules test NAME --json                # rows: id, p_hit, does, title, url
qualm rules label NAME --yes ID… --no ID… --json
qualm rules tune NAME --apply --json        # needs >= 3 yes and 3 no
qualm rules on NAME --json
```

Judge rows from their title and URL; when unsure, show the user the top
rows and ask. If the right pages don't score above the wrong ones, change
the wording (`--what`) and test again rather than forcing a threshold.
`rules test` needs the model server (`status`) and takes ~0.3 s a screen.

## Don't

- start, stop or restart the app or the model server without asking;
- remove rules, exceptions or never-here places the user didn't mention;
- use `--data` / `--rules` paths other than the defaults unless told to.
