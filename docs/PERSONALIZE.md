# Making Qualm yours, from the command line

**The easy way is an AI agent** that can run commands on your Mac (Claude
Code, Codex, Cursor). In Qualm's menu bar, *Change rules with your AI
agent…* copies a prompt for it, with the pop-ups you recently said were
wrong. Paste it into your agent and say what you want in your own words. The
prompt tells the agent to run `qualm guide`, and the guide makes it score
each change on your own screens before saving it.

Why not by hand: rules and exceptions are sentences a small model reads, and
it reacts to words, not intentions. On a real Mac, an exception written as
"chatting with friends in a messaging app such as WeChat, WhatsApp or
iMessage is fine" pushed WhatsApp's score on the social rule from ~0.17 to
~0.50: the opposite of what it said. What worked was an allow class, scored
on that Mac's last 760 screens first (chats 0.56–0.81, social media 0.16 or
less). The commands below make that kind of check possible; an agent runs
them for you.

Everything you can personalize is a command. The commands edit the same
files the app, the pop-up and the review page use: `rules.toml` for rules,
allow classes and settings, and `data/exceptions.jsonl` for what you taught
it at runtime, both in `~/Library/Application Support/Qualm`. The running app
picks up changes without a restart.

The commands are written to be driven by an agent as well as by hand
(Claude Code picks up `.claude/skills/qualm` in this repo):

- every command takes `--json`, errors included:
  `{"error": {"code": "over_limit", "message": "..."}}`;
- exit codes: 0 ok, 2 invalid, 3 not found, 4 over the question limit,
  5 app or model down;
- every change takes `--dry-run`: it prints the diff and saves nothing;
- every change is checked with the same checks as loading; a change that
  would break the file or go over the question limit is refused with a
  message that says what to do, and nothing is written;
- `config apply` makes many changes in one checked step, and `config undo`
  reverts the last saved change (one level: `rules.toml.bak`);
- `schema` lists every field with its type, default and meaning, and
  `status` says whether the app and the model are up.

`rules.toml` is created from the starter rules
(`src/qualm/rules.example.toml`) by `qualm setup` or the app's setup window,
or the first time any command runs.

## Rules

```bash
qualm rules list                   # every rule in a sentence: on/off, active now, today's usage
qualm rules show social --json     # every field, plus exceptions learned from the pop-up
qualm rules starters               # the ready-made rules, and which you have

# A rule in your own words. With --check-in it asks what for and how long; without, Qualm steps in.
qualm rules add news --what "reading news: news sites, articles and headlines" \
    --check-in --when "mon-fri 09:00-18:00" --site nytimes.com --on-purpose-ok
qualm rules add feeds --from-starter

# Change anything: key=value sets, key+=item / key-=item edit a list, key= removes a key.
qualm rules set news threshold=0.25 sites+=bbc.com 'what=news articles and headlines'
qualm rules set shortvideo 'when+=mon-fri 09:00-18:00'   # a creator: only during work hours
qualm rules off videos                                    # kept, but never asked
qualm rules on videos
qualm rules remove news
```

`what=` edits whichever description the model reads. The fields, as in
`rules.example.toml`:

| Field | Meaning | `rules add` flag |
|---|---|---|
| `description` | what the rule is about, in your words; the model reads it | `--what` |
| `kind` | `deny` (step in at once) or `check_in` (ask what for and how long on arrival; step in when that time is up) | `--check-in` |
| `sites` | always a hit here: `douyin.com` (and subdomains), `youtube.com/shorts` (and under it), `youtube.com/` (home page only) | `--site` (repeat) |
| `patterns` | the same as URL regexes | |
| `when` | only at these times: `"mon-fri 09:00-18:00"`, `"weekends"`, `"22:00-02:00"`; empty: always | `--when` (repeat) |
| `enabled` | `false`: kept, but never asked and never fires | `--off` |
| `allow_learning` | lectures, tutorials and docs never hit it | `--learning-ok` |
| `allow_intentional` | one item opened from search, a work app or a chat link is fine | `--on-purpose-ok` |
| `target` | `page`: judge the page itself, so feeds and home pages can hit | `--whole-page` |
| `feed_hit` | any page the model reads as an entertainment feed hits it | `--feeds` |
| `threshold` | the model's score that counts as a hit (default 0.2) | `--threshold` |
| `exceptions` | things that look like a hit but are fine; the model reads them | `except add` |
| `note` | why it's set this way, for whoever reads the file next | `--note` |

## The question limit

Each reading asks the model one question per rule that's on at that moment,
one per allow class, and 3 shared ones (sensitive, page kind, purpose). The
cost isn't flat past ~10 rules (HANDOFF, Measured): on Kev-4B on a 24 GB
Mac, 13 questions take 0.6 s, 28 take 1.8 s, 53 up to 18 s, and 103 timed
out and swapped the machine. So `[settings] max_questions = 25` is a hard
limit, checked over the whole week: rules whose `when` hours don't overlap
share a slot. `rules list` and every change print how full it is; a change
past it is refused (exit code 4) with the ways out: hours that don't
overlap, folding two rules into one description, or switching one off.

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
with what the rule would do there through the same exemptions as the live
policy (work tool, learning, opened on purpose, allow classes). Then say
which really are the rule, and let Qualm pick the threshold:

```bash
qualm rules label news --yes 6d159df9 0f437a14 --no 52a39a64 c1d80582 d4f385bc
qualm rules tune news              # the threshold that's right on 90% of hits, from your answers
qualm rules tune news --apply      # write it to rules.toml
```

Try wording before saving anything: `rules test news --what "news
headlines and articles"` tests it as a draft (for an existing rule, in
place of its saved wording; for a new id, as a new rule).

Scores are cached per exact wording (`data/trials.jsonl`), so running
`test` again is fast, and changing the description makes it ask again.
`tune` needs at least 3 yes and 3 no. The answers are the same review
answers the review page records, so they also feed `eval --reviews` and
`export`.

It takes about 0.3 s a screen on Kev-4B, and needs the model server. Adding
a rule with `--off`, testing and tuning it, then `rules on` is the safe
order.

## Everything else

```bash
# Kinds of page no rule fires on, in your words. Score a draft first:
# which of your recent screens it covers, and which pop-ups it would stop.
qualm allow test chat --what "a chat conversation in a messaging app" --last 500
qualm allow list
qualm allow add games --what "a video game or game launcher" --app com.valvesoftware.steam
qualm allow set music sites+=music.amazon.com
qualm allow off shopping

# Things that look like a rule but are fine
qualm except list                  # typed ones, and those learned from "Not this one"
# "Not this one" on a page with an address lets that address through. In an
# app window (no address), it lets that window through below the score it
# had, plus 0.1; `except list` shows the app and the bar.
qualm except add videos "a lecture or conference talk"
qualm except remove shortvideo "How to fix a bike chain"   # the text, page title or URL

# Apps and sites where no rule ever fires ("Never here" on the pop-up)
qualm never list
qualm never add --app net.whatsapp.WhatsApp --name WhatsApp
qualm never remove --site example.com

# Settings
qualm settings show
qualm settings set max_wait_s=90                 # the longest check-in wait, in seconds (default 60)
qualm settings set extensions=0                  # no "5 more" when a check-in's time is up
qualm settings set block_clicks=false            # the dim behind a pop-up lets clicks through (also in the menu bar)
qualm settings set no_monitor+=com.tencent.xinWeChat   # never read at all
qualm settings set allow_sites+=gitlab.com       # never judged; links from it count as on purpose
qualm settings set backend=jev                   # the model hosted by TypeSafe; kev: on this Mac (also in the menu bar)
qualm settings set keep_days=30                  # judgements kept 30 days (default 90; 0 keeps everything)
qualm settings set keep_shots_days=7             # screenshots kept 7 days (default 30)
```

## Many changes at once

```bash
qualm config export > cfg.json          # settings, allow classes and rules, as written
# edit cfg.json
qualm config apply cfg.json --dry-run   # the diff
qualm config apply cfg.json             # all of it, checked together, or nothing
qualm config undo                       # changed your mind
```

Entries missing from the file are kept: a partial list never deletes
anything. `--prune` removes what's missing. Near the question limit, this
is how to swap rules: switching one off and adding another in separate
commands would be refused halfway.

## Focus and pause

```bash
qualm focus write the report --minutes 50   # every rule hit steps in at once, check-ins included;
                                            # the pop-up says "You're here to: write the report."
qualm focus                                 # what's running, minutes left
qualm focus --stop
qualm pause 30                              # nothing is judged for 30 minutes
qualm pause --stop
```

Both live in `data/session.json`, which the running app reloads within a
second; the menu bar's "Start a focus session…" and "Pause" write the same
file. They work whether or not the app is running.

## Is it running

```bash
qualm status            # app, model, config and question budget, what it judged last (exit 5 if down)
qualm config check      # does rules.toml load
qualm schema            # every field and its grammar
```

## For an agent

`qualm guide` prints the operating guide (the same text as
`.claude/skills/qualm/SKILL.md`, which Claude Code picks up in this repo):
start with `status`, `rules list` and `schema`, map the user's words onto
fields, dry-run, respect the question limit, score every wording on the
user's screens (`rules test`, `allow test`) before saving it, and never put
app or site names into an exception to exclude them. A user saying "stop me watching streams during work, but
lectures are fine" maps to:

```bash
qualm rules set livestream 'when+=mon-fri 09:00-18:00' allow_learning=true --json
```

What the commands don't do yet: snooze one rule (the pop-up's "I need it"
does), and anything that needs the dashboard's screenshots.
