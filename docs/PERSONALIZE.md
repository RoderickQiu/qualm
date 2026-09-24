# Making Qualm yours, from the command line

**The easy way is an AI agent** that can run commands on your Mac (Claude
Code, Codex, Cursor). In Qualm's menu bar, *Change rules with your AI
agent…* copies a prompt for it, with the pop-ups you recently said were
wrong. Paste it into your agent and say what you want in your own words. The
prompt tells the agent to run `qualm guide`, and the guide makes it score
each change on your own screens before saving it. On a first day, with
nothing to score yet, it saves a first guess, tells you, and checks it once
you've used the Mac with Qualm for a day.

Why not by hand: rules and exceptions are sentences a small model reads, and
it reacts to words, not intentions. On a real Mac, an exception written as
"chatting with friends in a messaging app such as WeChat, WhatsApp or
iMessage is fine" pushed WhatsApp's score on the social rule from ~0.17 to
~0.50: the opposite of what it said. What worked was an allow class, scored
on that Mac's last 760 screens first (chats 0.56–0.81, social media 0.16 or
less). Naming sites in a rule narrows it the same way: the starter video
rule, worded "entertainment videos on Bilibili or YouTube", let Netflix and
Vimeo through. The commands below make that kind of check possible; an
agent runs them for you.

Everything you can personalize is a command. The commands edit the same
files the app, the pop-up and the review page use, both in
`~/Library/Application Support/Qualm`: `rules.toml` for rules, allow
classes, settings and the exceptions you type, and `data/exceptions.jsonl`
for what the pop-up taught it (*Not this one*, *Never here*). The running
app picks up changes without a restart.

The commands are written to be driven by an agent as well as by hand
(Claude Code picks up `.claude/skills/qualm` in this repo):

- every command takes `--json`, and answers in JSON, errors and usage
  mistakes included: `{"error": {"code": "over_limit", "message": "..."}}`;
  a command with nothing to say in JSON (`probe`, `app`, `watch`…) says so
  as an error;
- exit codes: 0 ok, 1 unexpected, 2 invalid, 3 not found (`no_screens`:
  nothing to test on yet), 4 over the question limit, 5 app or model down;
  every non-zero exit comes with that `error`, `status --json` included;
- on a Qualm folder other than the default (`QUALM_HOME`), every command a
  message suggests starts with `QUALM_HOME=…`, as it must: a plain `qualm`
  there would change the main folder;
- every change takes `--dry-run`: it prints the diff and saves nothing, even
  before rules.toml exists;
- every change is checked with the same checks as loading, value types
  included (lists are lists, `true` has no quotes, regexes compile); a change
  that would break the file or go over the question limit is refused with a
  message that says what to do, and nothing is written;
- each change is saved whole, under a lock, so commands run at the same time
  don't lose each other's changes;
- `config apply` makes many changes in one checked step, and `config undo`
  steps back (below). `backups/` next to rules.toml keeps the file as it
  was before each of the last 20 saved changes (`rules.toml.1`, `.2`, …,
  the highest number the newest; `rules.toml.bak` beside rules.toml is a
  copy of the newest), the file as Qualm last wrote it
  (`rules.toml.saved`), the file the last undo replaced
  (`rules.toml.replaced`) and any kept version an undo passed over
  (`rules.toml.N.aside`); Qualm never reads the last two;
- `schema` lists every field with its type, default and meaning, and
  `status` says whether the app and the model are up.

`rules.toml` is created from the starter rules
(`src/qualm/rules.example.toml`) by `qualm setup` or the app's setup window,
or the first time any command runs (`--json` says so under `warnings`).
You can still edit it by hand (the menu's *Edit rules file…*); a mistake is
reported with the file, the line and the fix, and `qualm config undo` goes
back to the version Qualm last saved, dropping only the hand edit (the
error names undo only when there's a version to go back to). Until the
mistake is fixed, the app keeps watching with the rules it last loaded
(when it starts: the version Qualm last saved, else an earlier saved
version, else the starter rules), and its menu bar icon and first menu
line say so; a file it can't read doesn't stop it either. Meanwhile it
never reads the apps the broken file lists in `no_monitor` (as far as a
lenient reading finds them), and on the hosted model it sends nothing to
TypeSafe until the file loads: a rule's own sites and apps still step in.
A rules.toml from an older Qualm, which kept no record of its last save,
is recorded as the last save the first time a command or the app finds it
loading.
Removing the last rule or allow class from a file with no `[settings]`
leaves a `[settings]` line, so the file still loads.

## Rules

```bash
qualm rules list                   # every rule in a sentence, plus allow classes, never-here places, exceptions
qualm rules list --pages           # also the pages Not this one let through (otherwise counted: not_this_one)
qualm rules show social --json     # every field, plus exceptions learned from the pop-up
qualm rules starters               # the ready-made rules, and which you have

# A rule in your own words. With --check-in it asks what for and how long; without, Qualm steps in.
qualm rules add news --what "reading news: news sites, articles and headlines" \
    --check-in --when "mon-fri 09:00-12:00, 13:00-18:00" --site nytimes.com
qualm rules add games --what "playing video games" --app Steam
qualm rules add feeds --from-starter   # a starter you removed, back as it shipped

# Change anything: key=value sets, key+=item / key-=item edit a list, key= removes a key.
qualm rules set news threshold=0.25 sites+=bbc.com 'what=news articles and headlines'
qualm rules set shortvideo 'when=["sat-sun", "mon-fri 18:00-24:00"]'   # a creator: not during work hours
qualm rules off videos                                    # kept, but never asked
qualm rules on videos
qualm rules remove news
```

`what=` edits whichever description the model reads. The fields, as in
`rules.example.toml`:

| Field | Meaning | `rules add` flag |
|---|---|---|
| `description` | what the rule is about, in your words; the model reads it | `--what` |
| `description_en` | an English version, which the model reads instead (`lang = "en"`, the default); English separates best on the local model | |
| `kind` | `deny` (step in at once) or `check_in` (ask what for and how long on arrival; step in when that time is up) | `--check-in` |
| `sites` | always a hit here, even while the model is down: `douyin.com` (and every subdomain: `mail.`, `email.` too), `youtube.com/shorts` (and under it), `youtube.com/` (home page only); any letter case, any script (`straße.de` matches both `xn--strae-oqa.de`, which browsers send, and `strasse.de`). Except an allow class's own listed sites: `youtube.com` leaves the music class's `music.youtube.com` alone, unless the rule names that site (or a narrower one) or overrides the class | `--site` (repeat) |
| `patterns` | the same as URL regexes | |
| `apps` | always a hit for this rule while this app is in front, without the model's say (the other rules are still judged there); naming a browser makes every page in it a hit. App names, bundle ids or .app paths (`Steam`, `com.valvesoftware.steam`, `/Applications/Steam.app`), saved as the installed app's bundle id | `--app` (repeat) |
| `when` | only at these times: `"mon-fri 09:00-18:00"`, `"mon-fri 09:00-12:00, 13:00-18:00"`, `"weekends"`, `"22:00-02:00"`; empty: always. Different days with different hours are separate entries | `--when` (repeat) |
| `enabled` | `false`: kept, but never asked and never fires | `--off` |
| `allow_learning` | lectures, tutorials and docs never hit it | `--learning-ok` |
| `allow_intentional` | one item opened from search, a work app or a chat link is fine, except on the rule's own sites | on for `rules add`; `--no-on-purpose-ok` |
| `target` | `page`: judge the page itself, so feeds, home pages and store fronts can hit; `content`: only the item that's open (search results wait for what you open from them, as for check-in rules) | `page` for `rules add`; `--content-only` |
| `feed_hit` | any page the model reads as an entertainment feed hits it | `--feeds` |
| `overrides_allow` | allow classes this rule steps in on anyway (`shopping` for a shopping rule), on the class's listed sites and apps too; set by `rules add` when the rule's English words name one. A name that isn't an allow class is refused, and ignored if it's already in the file | |
| `threshold` | the model's score that counts as a hit (default 0.2) | `--threshold` |
| `exceptions` | things that look like a hit but are fine; the model reads them | `except add` |
| `note` | why it's set this way, for whoever reads the file next | `--note` |

A rule's id is its name in the pop-up and the menu ("online_shopping" shows
as "Online shopping"; a rule described in letters an id can't hold, like
"刷短视频", goes by its description's first words), and the pop-up quotes
its description up to the first comma or colon, so keep that first clause a
short phrase. Ids are lowercase ASCII, and rules and allow classes share
them: an allow class can't take a rule's id.

`rules add`, and `rules set` when it changes a description, list the allow
classes that can still let pages through whatever the rule says
(`can_be_cancelled_by` in JSON; "Allow classes win over it: …" in text).
Qualm sets `overrides_allow` by itself only where the rule's English words
name a class and say nothing anywhere about what the rule isn't about, in
either description: not, no, but, except, other than, without, unless,
rather than, non-, 不, 除了 and the like. "Videos other than music or chat"
overrides nothing; the classes such words name come back as `may_cancel`,
each with the command that sets it, to run only if the rule really is about
those pages too. A rule in another language, or an overlap no word names (a
game store and shopping, dating and chat), needs
`qualm rules set ID overrides_allow+=shopping`. `allow remove` of a class a
rule names is refused until `rules set ID overrides_allow-=CLASS` has run.

## The question limit

Each reading asks the model one question per rule that's on at that moment,
one per allow class, and 3 shared ones (sensitive, page kind, purpose). The
cost isn't flat past ~10 rules (HANDOFF, Measured): on Kev-4B on a 24 GB
Mac, 13 questions take 0.6 s, 28 take 1.8 s, 53 up to 18 s, and 103 timed
out and swapped the machine. So `[settings] max_questions = 25` is a hard
limit (4 at least), checked over the whole week: rules whose `when` hours
don't overlap share a slot. `rules list` and every change print how full it
is; a change past it is refused (exit code 4) with the ways out: hours that
don't overlap, folding two rules into one description, or switching one off.

## Test a rule before you trust it

A new rule, or new wording, hasn't been measured. Kev-4B ranks pages well,
but its scores run low (the tuned thresholds are 0.15-0.5), so the default
0.2 is a guess. Try it on screens you've actually had:

```bash
qualm rules test news              # asks the model this one rule on your last 100 distinct screens
```

```
news on your last 25 distinct screens, at threshold 0.2: 0 would pop up.

#6d159df9  0.09  nothing
      - Google Chrome  https://xiaohongshu.com/
…
```

Each row is a screen from `data/judgements.jsonl`, highest score first,
with what the rule would do there through the same checks as the live
policy (work tool, learning, opened on purpose, allow classes, the rule's
hours, never-here places, pages you marked fine), and why. It asks the
model exactly what the app asks, with what *Not this one* and the dashboard
taught. Then say which really are the rule, and let Qualm pick the
threshold:

```bash
qualm rules label news --yes 6d159df9 0f437a14 --no 52a39a64 c1d80582 d4f385bc
qualm rules tune news              # the threshold that's right on 90% of hits, from your answers
qualm rules tune news --apply      # write it to rules.toml
```

`tune` never suggests a threshold that does worse on your answers than the
one you have; then it says to keep it. `--precision 0.95` asks for fewer
wrong pop-ups, a lower value catches more (a share between 0 and 1). It
needs at least 3 yes and 3 no: with fewer it says "too few answers to tune
it" (JSON `enough_answers: false`). It uses only scores for the current
wording and model, saying what it left out (`dropped`). Answers from
outside a rule's `when` are left out too (`outside_hours`), since no
threshold changes anything there: for an evening-only rule, label screens
from its hours. `rules test` says so too: in the daytime, it says an
evening rule's screens are all from outside its hours (JSON
`outside_hours`) and asks for no answers. Answers logged before
Qualm noted the wording and model count as the current wording and this
home's model, unless the rule was reworded since (the log or rules.toml's
kept versions show it).

Try wording before saving anything: `rules test news --what "news
headlines and articles"` tests it as a draft (for an existing rule, in
place of its saved wording; for a new id, as a new rule).

Scores are cached per exact wording and model (`data/trials.jsonl`), so
running `test` again is fast, a run cut short picks up where it stopped, and
changing the description makes it ask again. If the model stops answering
partway, `test` shows what it scored and says it "stopped answering after N
of M" (JSON `complete: false`, exit 5): the same command again goes on from
there. If the model answers nothing at all, it says it "didn't answer", with
any scores from earlier runs: check `qualm status` before trying again. The
answers are the same review answers the review page records, so they also
feed `eval --reviews` and `export`.

On Kev-4B it takes 0.5-1.5 s a screen (more when the Mac is short of
memory), and the app's own readings wait meanwhile, so keep `--last` small
(50-100) at first; hosted, about 0.2 s. It needs the model. Adding a rule
with `--off`, testing and tuning it, then `rules on` is the safe order.

**On a first day** there's nothing to test on (`no_screens`, exit 3), or no
screen of the kind the rule is about. Save it with its first-guess
threshold, knowing it's untested (or `--off`), use the Mac with Qualm
running for a day or two, then test and tune it. Sites, apps and hours need
no testing: they always count.

## Everything else

```bash
# Kinds of page no rule fires on, in your words. Score a draft first:
# which of your recent screens it covers, and which pop-ups it would stop.
qualm allow test calls --what "a video call or online meeting" --last 100
qualm allow list
qualm allow add calls --what "a video call or online meeting" --app zoom.us   # names resolve to bundle ids
qualm allow set music sites+=music.amazon.com
qualm allow off shopping

# Things that look like a rule but are fine
qualm except list                  # typed ones, and those learned from "Not this one"
# "Not this one" on a page with an address lets that address through. In an
# app window (no address), it lets that window through below the score it
# had, plus 0.1; `except list` shows the app and the bar. Neither changes
# what the model reads.
qualm except add videos "a lecture or conference talk"
qualm except add social --from 6d159df9    # that one screen was fine: what Not this one does
                                           # (also a pop-up's decision id; on a rule's own app, that window at any score)
qualm except remove shortvideo "How to fix a bike chain"   # the text, page title or URL
# An address takes back just that page. A title several pages share
# ("Instagram") is refused with a request for the address.

# Apps and sites where no rule ever fires ("Never here" on the pop-up)
qualm never list
qualm never add --app WhatsApp                    # an app name or a bundle id
qualm never remove --site example.com

# Settings
qualm settings show
qualm settings set max_wait_s=90                 # the longest check-in wait, in seconds (default 60)
qualm settings set extensions=0                  # no "5 more" when a check-in's time is up
qualm settings set block_clicks=false            # the dim behind a pop-up lets clicks through (also in the menu bar)
qualm settings set no_monitor+=WeChat            # never read at all (password managers never are); -=WeChat removes it
qualm settings set allow_sites+=gitlab.com       # never judged; links from it count as on purpose
qualm settings set backend=jev                   # the model hosted by TypeSafe (needs a saved key: `qualm setup --backend jev`); kev: on this Mac
qualm settings set hf_endpoint=https://hf-mirror.com   # a Hugging Face mirror (https), where huggingface.co is blocked
qualm settings set dashboard_port=8766           # the dashboard's port (8765 by default)
qualm settings set keep_days=30                  # judgements kept 30 days (default 90; 0 keeps everything)
qualm settings set keep_shots_days=7             # screenshots kept 7 days (default 30)
```

The dashboard's Rules tab adds and removes typed exceptions too (the same
as `except add` / `except remove`, 200 characters at most).

## Many changes at once

```bash
qualm config export > cfg.json          # settings, allow classes and rules, as written
# edit cfg.json
qualm config apply cfg.json --dry-run   # the diff
qualm config apply cfg.json             # all of it, checked together, or nothing (- reads stdin)
qualm config undo                       # changed your mind: one step back per undo
```

`config apply` with `backend = "jev"` needs a saved key (exit 2), as
`settings set` does; a key only in this shell's TYPESAFE_API_KEY is taken
with a warning, since Qualm.app won't see it. It doesn't resolve app names
to bundle ids the way `--app` and `apps+=` do.

Only what the file names changes: rules, allow classes, settings and keys
it leaves out are kept, so a partial list never deletes anything, and a key
set to `null` goes back to its default. `--prune` removes what's missing,
keys included. Near the question limit, this is how to swap rules: switching
one off and adding another in separate commands would be refused halfway.

`config undo` works in two steps. If rules.toml was changed by hand since
Qualm last saved it (broken or not), it goes back to the version Qualm last
saved (JSON `undid: "hand_edit"`), and the saved versions stay: the first
undo after a hand edit drops only the hand edit. Otherwise it steps back one
saved change (`"saved_change"`), through the last 20. It shows what it put
back. Every undo keeps the file it replaces as `backups/rules.toml.replaced`
(`replaced_file`), so a hand edit is never lost; there's no redo command,
but that file is the last undo's redo, by hand. A kept version that no
longer loads is passed over, named (`skipped`) and set aside as
`backups/rules.toml.N.aside` (`kept_aside`), not deleted; if none loads,
nothing changes, and the error says which file to correct. `--dry-run`
shows all of it (`undid`, `skipped`, `kept_aside`, `replaced_file`)
without saving. On a home an older Qualm set up, the first command records
rules.toml as the last save; if rules.toml was already broken before that,
there's no such record, so undo goes back to the version before the last
saved change, and `note` says how to keep that change. Deleting rules.toml
and letting Qualm make it again from the starters can be undone too. It
doesn't touch the rest: `never remove`, `except remove`, `focus --stop` and
`pause --stop` undo those.

## Focus and pause

```bash
qualm focus write the report --minutes 50   # every rule hit steps in at once, check-ins included;
                                            # the pop-up says "You're here to: write the report."
qualm focus                                 # what's running, minutes left
qualm focus --stop
qualm pause 30                              # nothing is judged for 30 minutes; also 2h, 2d
qualm pause --until 'mon 09:00'             # from now until Monday 09:00 (a week at most): a weekend under way
qualm pause --from 'sat 00:00' --until 'mon 09:00'   # the coming weekend off, planned on a weekday
qualm pause --from 'mon 00:00' --until 'sat 00:00'   # next week off
qualm pause --stop                          # resume, and cancel a planned pause
```

`--from` is always its next occurrence, and `--until` the next one after
it. So the weekend recipe run on a weekday, Monday before 09:00 included,
plans the coming weekend; run on Saturday or Sunday it plans the next one,
and says that `pause --until 'mon 09:00'` pauses the one under way. A
`--from` already past, or more than 7 days ahead, is refused with the
reason; one in the last 5 minutes counts as now. A pause lasts at most 7
days, and a second `--from` replaces the first. The menu has "Cancel the
pause from … to …" while one is planned, and its status line says when it
begins; the dashboard's Today card mentions it.

All of it lives in `data/session.json` (`paused_until`, `pause_later` for a
planned pause, `focus`), which the running app reloads within a second; the
menu bar's "Start a focus session…" and "Pause" write the same file. They
work whether or not the app is running (an app older than planned pauses
ignores `pause_later`, and `pause --from` warns). Starting a focus session
ends the pause running now, not a planned one. In a focus session the
pop-up's "It's part of the task" lets that page through until the session
it was shown in ends; clicked after that session ended, it saves nothing,
and a *Not this one* shown before a session started is saved for good.

## Is it running, and how is it going

```bash
qualm status            # app, which model and whether it answers, config and question budget, last judgement
                        # exit 2 if rules.toml doesn't load, 5 if the app or model is down; --json's error lists each
                        # a model server too busy to answer in time counts as up ("busy answering"); model.why says
                        # why one doesn't answer: remote (KEV_URL elsewhere), starting, taken (another app) or none
qualm doctor            # everything Qualm needs, each problem with its fix
qualm config check      # does rules.toml load
qualm schema            # every field and its grammar
qualm week              # this week in numbers: the dashboard's Insights
qualm review --since 2026-09-22 --rule social   # what it did and why, with scores, your answers and how each pop-up ended
                        # --since also takes today, yesterday, '2026-09-22 18:00'; the newest 100 from then,
                        # without it the newest 30. When it cuts, JSON has truncated: true and a note:
                        # --last N for more, --rule R or --acted (only where Qualm did something) for fewer
qualm review --id 1a2b3c4d5e6f   # one pop-up, by the decision id the menu's prompt for an agent names
qualm review --misses   # the screens you said it should have caught (not the ones you chose to let through)
```

## For an agent

`qualm guide` prints the operating guide (the same text as
`.claude/skills/qualm/SKILL.md`, which Claude Code picks up in this repo):
start with `status`, `rules list` and `schema`, map the user's words onto
fields, dry-run, respect the question limit, score every wording on the
user's screens (`rules test`, `allow test`) before saving it, save a first
guess and say so when there's nothing to score yet, and never put app or
site names into an exception to exclude them. A user saying "stop me
watching streams during work, but lectures are fine" maps to:

```bash
qualm rules set livestream 'when+=mon-fri 09:00-18:00' allow_learning=true --json
```

What the commands don't do yet: snooze one rule (the pop-up's "I need it"
does), and anything that needs the dashboard's screenshots.
