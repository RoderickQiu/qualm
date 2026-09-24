# Driving Qualm for the user

Run `qualm` the way the prompt that sent you here says: `qualm` if it's on
PATH, else `~/.local/bin/qualm` (the command Qualm.app added),
`"/Applications/Qualm.app/Contents/MacOS/Qualm" -m qualm`, or
`uv run --project <repo> qualm` from a checkout of the repo. `qualm guide`
prints this. The rules and data live in `~/Library/Application Support/Qualm`,
whichever folder you run it from. For another Qualm folder the prompt's
command starts with `QUALM_HOME=…`, and so does every command Qualm's own
messages suggest: keep it, since a plain `qualm` there changes the main one.

Always pass `--json` and read the result. Every command answers in JSON,
usage errors included: `{"error": {"code", "message"}}` with exit codes
2 invalid, 3 not found (code `no_screens`: nothing to test on yet),
4 over the question limit, 5 app or model down (code `unreachable`),
1 unexpected. Don't edit rules.toml directly: the commands check every
change (a broken or over-limit file is never saved) and keep the last 20
versions. Independent commands may run at the same time; each change is
locked and saved whole.

Answer the user in the language they write in. `rules test` and
`allow test` print the titles and addresses of their recent screens, and
those reach you and your provider: ask for no more screens than the job
needs, quote back only the ones you need, and tell a user who wants nothing
to leave their Mac before you run them.

## Measure, don't guess

Rules and exceptions are sentences a small model reads, and it reacts to
words, not intentions. Wording that reads right can make things worse: an
exception naming the apps to leave alone ("chatting in WeChat, WhatsApp or
iMessage is fine") pushed WhatsApp's score on the social rule from ~0.17 to
~0.50. So never save a wording you haven't scored on the user's own screens
(`rules test`, `allow test`) when there are screens to score it on, and
report the numbers when you're done: what scored what, before and after.
When there aren't, see "The first day" below.

Rule text in English separates best on the local model. For a rule in
another language, keep the user's words in `description` and add an English
version as `description_en`, which is what the model reads (`lang = "en"`).

## Start here

```bash
qualm status --json        # app running? which model, and does it answer? config ok? question budget
qualm rules list --json    # "what are my rules?": rules, allow classes, never-here places and exceptions
qualm schema --json        # every field: type, default, meaning; the grammar of sites and when
```

`rules list --json` counts the pages let through with *Not this one* per
rule (`not_this_one`), without their titles and addresses; `rules list
--pages` or `except list` shows them, and like `rules test` that output
reaches your provider. `warnings` says when a command just created
rules.toml from the starter rules.

`status` has `backend` (`kev` on this Mac, `jev` hosted; for jev,
`model.key` and whether TypeSafe answers), and `app_running` is true only
for the app using this Qualm folder. It exits 2 when rules.toml doesn't
load and 5 when the app or the model is down, and then `error.message`
lists each (`config: …; app: …; model: …`). When the local model doesn't
answer, `model.why` says why: `remote` (`KEV_URL` on another machine),
`starting` (the app's server is downloading or loading it), `taken`
(another app holds the port) or `none`, with `model.error` in words.
`qualm doctor --json` checks everything, with the fix for each; its
"Setup" check fails until setup ran.

If `status` has `app_older: true`, the app running now is an older copy of
Qualm: ask the user to quit it from its menu bar icon and open it again
before you change anything. An older app can't load keys newer commands
write (`apps`, `overrides_allow`): it keeps its old rules, while the
command still reports success.

Every command that asks the local model (`status`, `doctor`, `rules test`,
`allow test`) finds its server the same way: `KEV_URL`, else the port the
running app's server moved to (noted in `models/server.json` while the app
runs, when another app holds 8009), else 8009. A model server too busy to
answer in time counts as up (`model.busy: true`, slow on a Mac short of
memory): wait, don't restart it. With the app not running and another app
on 8009, model commands refuse ("port 8009 is used by another app…") and
send nothing: ask the user to open the app.

## Setting up

- `qualm setup --backend kev|jev --rules a,b --json` sets up without
  questions (`--dry-run` first shows what it would do). It leaves start at
  login as it is; `--login` turns it on, so ask the user first.
- The hosted model (jev) needs a TypeSafe account and key
  (console.typesafe.ai). Never put a key in a command line: ask the user to
  run `qualm setup --backend jev` in their own terminal, or to use the menu
  bar (Model > "Hosted by TypeSafe (Jev)…"). A script can pass it on stdin
  (`--key -`) or in TYPESAFE_API_KEY. `settings set backend=jev` and
  `config apply` with `backend = "jev"` are refused without a key. A key
  only in TYPESAFE_API_KEY is taken with a warning: Qualm.app doesn't see
  it, and `setup --backend jev --key -` saves it in the keychain.
- The local model (kev) needs Apple silicon and macOS 14 or later (setup
  refuses it elsewhere), and downloads about 6 GB on its first start (the
  model and its runtime). Where huggingface.co is blocked:
  `settings set hf_endpoint=https://hf-mirror.com` (an https address);
  mirrors for its Python packages go in `~/.config/uv/uv.toml` (`index-url`,
  `python-install-mirror`). Nothing there redirects the runtime's Kev
  archive on GitHub: where GitHub is blocked, ask the user to run
  `QUALM_KEV_ARCHIVE=<a copy of that archive> qualm serve` once in a
  terminal (the archive's address is in the error); the app uses that
  runtime afterwards.

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
  FILE --json` (`-` reads the JSON from stdin). All of it is checked and
  saved together, or not at all, and it's one step for `config undo`. Only
  what the JSON names changes: rules, allow classes, settings and keys it
  leaves out are kept, and a key set to `null` goes back to its default.
  `--prune` removes the entries and keys it leaves out: use it only after
  the user agreed to what the dry run's diff removes.
- Any change: add `--dry-run` first when unsure (it works before rules.toml
  exists). `config undo --json` returns
  `{undone, undid, restored, left, diff}`, and `--dry-run` shows the same
  without saving. If rules.toml was changed by hand since Qualm last saved
  it (broken or not), undo goes back to the version Qualm last saved and
  `undid` is `hand_edit`: only the hand edit is dropped. Otherwise it steps
  back one saved change (`saved_change`), through the last 20. Read `undid`
  and the diff to see what came back. The file undo replaces is kept as
  `backups/rules.toml.replaced` (`replaced_file`), so a hand edit is never
  lost. `skipped` lists kept versions it passed over because they no longer
  load, set aside as `backups/rules.toml.N.aside` (`kept_aside`), not
  deleted; an error "nothing to undo that loads" (`not_found`) means none
  does: correct the file it names, or rules.toml by hand
  (`qualm config check` says where). On a home an older Qualm set up, whose
  rules.toml was already broken before any newer command ran, there's no
  record of the last save: undo goes back to the version before the last
  saved change, and `note` says how to keep that change. A load error names
  `config undo` only when there's a version to go back to. There's no redo:
  another undo goes further back, and `rules.toml.replaced` holds what the
  last one replaced. It covers rules.toml only: `never remove`,
  `except remove`, `focus --stop` and `pause --stop` undo the rest.

Map the user's words onto fields, e.g. "no streams during work, lectures
are fine" → `rules set livestream 'when+=mon-fri 09:00-18:00' allow_learning=true`.
Describe modes, not sites: "short videos made for endless swiping", with
sites (`douyin.com`, `youtube.com/shorts`) only for what must always count.

- `when`: `"mon-fri 09:00-12:00, 13:00-18:00"` (several hours after the
  days), `"weekends"`, `"22:00-02:00"`. Different days with different hours
  are separate entries: `when=["mon 09:00-12:00", "tue 13:00-18:00"]`.
- `sites`: plain domains, in any letter case or script (`bücher.de`);
  `youtube.com/` alone is the home page. A domain covers every subdomain:
  `t-online.de` is also the webmail at `email.t-online.de`, so check what a
  site entry takes in before saving it.
- `apps`: "block Steam", "games", "the TV app" → `rules add games --what
  "video games" --app Steam` (or `rules set ID apps+=Steam`): always a hit
  for that rule while that app is in front, without the model's say; the
  other rules are still judged there as in any app. Naming a browser makes
  every page in it a hit for that rule. Every app list set from a command
  (`rules add --app`, `rules set ID apps+=`, `allow add --app`, `allow set
  ID apps+=`, `settings set no_monitor+=`, `never add --app`) saves the
  installed app's bundle id; a .app path works too
  (`/Applications/Figma.app`). If nothing installed matches, it saves the
  name as typed and says so under `warnings`. `-=Name` removes an entry
  saved either way. `config apply` doesn't resolve names.
- Pick ids that read as names (`news`, `online_shopping`): the pop-up and
  the menu call a rule by its id, underscores as spaces (a rule described
  in letters an id can't hold, Chinese or é, by its description's first
  words; ids stay lowercase ASCII). A rule you add is quoted in the pop-up
  up to its first comma or colon ("This looks like it's under your rule:
  news sites and headlines."), so keep that first clause a short phrase.

Two kinds: `deny` steps in at once; `check_in` asks what for and for how
long (5/15/30 min) on arrival and steps in when that time is up. "Limit my
YouTube to 30 minutes a day" → a `check_in` rule: there are no daily
budgets (docs/POLICY.md says why); say so, and offer `max_wait_s` or
`extensions=0` if they want it firmer. An old rules.toml with `time_cap`
or `budgets` won't load: `qualm config migrate --dry-run --json`, then
without `--dry-run`.

`rules add` judges the whole page by default, so a news front page, a
store or a scoreboard can hit; `--content-only` judges only the item that's
open (feeds and listings never hit, and search results wait for what the
user opens from them, as they do for check-in rules). It lets one item the
user opened on purpose through (from a search, a work tool, a link in a
chat); `--no-on-purpose-ok` counts those too. Ask when the user's words
don't settle it: "no news" usually means front pages too; "a link a
colleague sends is fine" is the default.

Allow classes (`allow list`: shopping, music, chat to start with) let a
kind of page through whatever a rule scores, and their listed sites and
apps (Spotify, open.spotify.com) without asking the model. A rule that
lists a class in `overrides_allow` steps in on its pages anyway; one that
names that very app, or a site as narrow as the class's or narrower
(`music.youtube.com`, not `youtube.com`), steps in on that place. A rule
on a whole domain leaves the class's listed subdomains to the class, and
every other rule still lets those pages through.
`rules add`, and a rewording (`what=`, `description_en=`), set
`overrides_allow` only where the rule's English words name a class
("online shopping", not "Einkaufen") and say nothing about what it isn't
about, and say so (`overrides_allow_added`). Words like not, no, but,
except, other than, without, unless, rather than, non-, 不 or 除了 anywhere
set nothing: "videos other than music or chat" overrides nothing, and a
class the words name then goes in `may_cancel`, with the command that
makes the rule step in there too; run it only if the rule really is about
those pages. Their JSON also lists `can_be_cancelled_by`: the
classes that can still let pages through whatever the rule says, with
`overrides_allow_note`. Read that list and judge the overlap yourself, in
the user's language: a German "Einkaufen" rule →
`rules set einkaufen overrides_allow+=shopping`; a game-store rule and
shopping; a dating rule and chat. `overrides_allow-=chat` takes one out,
`overrides_allow=` clears it; a name that isn't an allow class is refused.
`allow remove` of a class a rule names is refused until that rule's
`overrides_allow-=` runs. An evening-only shopping rule is
`rules add evening_shopping --what "online shopping" --when 20:00-24:00`.

## The first day: nothing to score yet

On a new install, or for something the user hasn't done on this Mac yet,
there's nothing to test on: `rules test` says `no_screens` (exit 3), or no
recent screen is of that kind. That's not the model failing, and it's no
reason to save nothing:

1. Save what needs no model now: hours, never-here places, and the sites
   and apps the user named (or a starter's). Never invent a site list: a
   site steps in without the model on every page and subdomain of it, a
   webmail or a login page included. If a list would help ("news sites"),
   propose one and save it only once the user agreed to each entry.
2. Save model-judged wording switched on, with its first-guess threshold
   (0.2), and tell the user it's untested; add it `--off` instead if they'd
   rather have no untested pop-ups. Where a starter rule fits, its wording
   and threshold were measured.
3. Say when to come back: after a day or two of normal use with Qualm
   running, `rules test` it, then label and tune (below).

Don't start the app for this without asking.

## Why did it pop up? Why didn't it?

- "Why did it pop up?" → for a pop-up the menu's prompt names,
  `qualm review --json --id DECISION` (its decision id, or a judgement id);
  otherwise `qualm review --json --since today` (also `yesterday`,
  `2026-09-22`, `'2026-09-22 18:00'`; `--rule R` for one rule). Each row has
  its date, what Qualm did and why, the scores and thresholds, the user's
  answers, and `popup` (`{id, rule, ended}`: how the pop-up ended, `back`,
  `session`, `snooze`, `fine`, `never`, `open` for no answer…). With
  `--id` every matching row comes back; with `--since` the newest 100,
  without it the newest 30: `matching` counts them all, `shown` what came
  back, and when it's cut `truncated` is true and `note` says how to get
  more (`--last N`) or fewer (`--rule R`, `--acted`: only where Qualm did
  something).
- "That page was fine" after the fact → `qualm except add RULE --from ID
  --json`, with a screen id from `rules test` or `review`, or the decision
  id in the menu's prompt. It does exactly what the pop-up's *Not this one*
  does, on a rule's own apps too (that window is let through at any score);
  another rule's pop-up is refused with the right command.
- "It missed X" → `qualm review --misses --json` lists the screens the user
  said it should have caught (the menu's "This should have been blocked",
  or a yes on the dashboard); screens a rule let through because the user
  chose so at the time (a check-in session, I need it, Not this one, opened
  on purpose, part of the task) aren't listed. Then label them
  (`rules label RULE --yes ID`, or `review --fix ID RULE=yes`), add a site,
  or reword and test.

## Something pops up where it shouldn't

1. Find it: `qualm rules test RULE --last 100 --json` lists recent screens
   (`screens`) with their scores, highest first. Note where the wrong ones
   sit against the threshold, and where the right hits sit.
2. One place (a window, a page): the pop-up's *Not this one* already
   handles it. On a page with an address it lets that exact address
   through (the same page with another query string is another address);
   in an app window it lets that window through below the score it had
   plus 0.1 (`except list` shows these; `except remove RULE ADDRESS` takes
   one page back, by its address, since titles repeat across pages). It
   doesn't change what the model reads.
   *Never here* silences a whole app or site.
3. A kind of page (chats, a store, video calls): an allow class, not an
   exception. Check `allow list` first, then draft a new one and score it
   before saving:
   `qualm allow test calls --what "a video call or online meeting: names of
   the people in it, mute and leave buttons" --last 100 --json`.
   Keep it only if the pages it should cover score above its threshold,
   the pages the user wants flagged score well below, and the pop-ups it
   would stop (`would_stop` counts them; each screen's `stepped_in` names
   the rule) were all wrong. Then `allow add ID --what "…" --threshold T`.
   Each allow class is one more question per reading (the limit, above).
4. A rule that's too broad or too narrow everywhere: new wording through
   `rules test NAME --what "…"`, then `label` and `tune`, as below.

"Fewer pop-ups" or "stricter" means a higher threshold, or
`rules tune NAME --precision 0.95`; "catch more" a lower one, or a lower
`--precision`. Check either with `rules test`.

Don't paste app or site names into exception text or a rule's wording to
exclude them: see "Measure, don't guess".

## Focus and pause

"I need to write the report for an hour, keep me on it" →
`qualm focus write the report --minutes 60 --json`: until it ends,
every rule hit steps in at once (check-in rules too) and the pop-up names the
intent; its "It's part of the task" lets that page through until the
session it was shown in ends (clicked after that, it saves nothing).
Starting one ends a pause running now, not a planned one. `qualm focus
--json` shows it, `--stop` ends it. "Leave me alone for 20 minutes" →
`qualm pause 20 --json`; `2h`, `2d` and `--until 'tomorrow 09:00'` work
too (a week at most). "This weekend" → on a weekday (Monday before 09:00
too) `qualm pause --from 'sat 00:00' --until 'mon 09:00' --json`; on
Saturday or Sunday `qualm pause --until 'mon 09:00' --json`. `--from` is
always its next occurrence and `--until` the next one after it, so the
`--from` form run during a weekend plans the next one, and `warnings` says
so with the command that pauses the one under way. "Next week off" →
`--from 'mon 00:00' --until 'sat 00:00'`. A `--from` already past, or more
than 7 days ahead, is refused with the reason (one in the last 5 minutes
counts as now). The JSON has `pause_later` (`{from, until}`), and `paused`
stays false until it begins; a second `--from` replaces the first, and a
warning says when the running app is an older copy that knows no planned
pauses. For a one-off, pause: don't save a lasting `when` change unless the
user asks for one. `pause --stop` resumes and also cancels a planned pause.
These don't touch rules.toml and need no dry run.

"How did I do this week?" → `qualm week --json`: the dashboard's Insights
numbers (`this_week` with pop-ups, how they ended and per rule;
`last_14_days`, `per_week`, `check_ins`, `focus`, `time_asked_for`,
`reviews`, `question`, `screens_read_today`).

## A new rule: test before trusting it

A new rule's threshold (0.2) is a guess. When there are screens to test on:

```bash
qualm rules test NAME --what "…" --json     # a draft: nothing saved
qualm rules add NAME --what "…" --off --json
qualm rules test NAME --json                # screens: id, p_hit, does, why, title, url
qualm rules label NAME --yes ID… --no ID… --json
qualm rules tune NAME --apply --json        # needs >= 3 yes and 3 no
qualm rules on NAME --json
```

`rules test --json` returns `rule`, `draft`, `reads`, `summary`,
`threshold`, `overrides_allow`, `backend`, `would_fire`, `outside_hours`
(screens from outside the rule's `when`, which tune leaves out), `complete`
and `screens`. Each screen has `id`, `at`, `p_hit`, `does`, `why`,
`overrides`, `app`, `title`, `url`, `page_kind`, `purpose` and `you_said`. `does` is "pops up" or
"checks in" for a hit (`would_fire` counts both), else "allow", "skip" or
"nothing", with `why` ("shopping is never flagged", "outside its hours:
mon-fri", "you marked this page fine", "search results"). It asks exactly
what the live app asks, with what *Not this one* and the dashboard taught.

Judge screens from their title and URL; when unsure, show the user the top
ones and ask. If the right pages don't score above the wrong ones, change
the wording (`--what`) and test again rather than forcing a threshold.

On the local model each screen takes 0.5-1.5 s (more when the Mac is short
of memory), and the app's own readings wait meanwhile: start with
`--last 50` to `100`. Scores are kept per wording, so a run cut short picks
up where it stopped, and a bigger `--last` only asks the new screens.
Hosted, a screen takes ~0.2 s. If the model stops answering partway,
`rules test` and `allow test` exit 5 with `complete: false`, an
`unreachable` error and the screens scored so far. "Stopped answering
after N of M": run the same command again to go on from there. "Didn't
answer" (the screens, if any, are scores from before): the model isn't
running; check `qualm status` and tell the user rather than retrying.

`rules tune --json` returns `backend`, `scored_by_test`, `scored_live`,
`dropped` (`other_wording`, `other_model`, `not_scored`, `outside_hours`)
and `keep_current`: true means leave the threshold as it is;
`enough_answers` false means fewer than 3 yes or 3 no, too few to tune
on: label more before touching the threshold. Answers from
outside a rule's `when` are left out, since no threshold changes anything
there: for an evening rule, label screens from its hours. Answers logged
before Qualm recorded wording and model count as the current wording and
the home's model, unless the rule was reworded since. After changing what
a rule means, label again (explicit answers are kept) and `rules test` the
new wording, so tune has scores for it.

## Don't

- start, stop or restart the app or the model server without asking;
- remove rules, exceptions or never-here places the user didn't mention;
- put a TypeSafe key in a command line;
- use `--data` / `--rules` paths other than the defaults unless told to.
