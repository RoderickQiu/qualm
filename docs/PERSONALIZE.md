# Making SeeNot yours, from the command line

Everything you can personalize is a command. The commands edit the same
files the app, the pop-up and the review page use: `rules.toml` for rules,
allow classes and settings, and `data/exceptions.jsonl` for what you taught
it at runtime. The running app picks up changes without a restart.

The commands are written to be driven by an agent as well as by hand
(Claude Code picks up `.claude/skills/seenot` in this repo):

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

`rules.toml` is created from the starter rules (`rules.example.toml`) the
first time any command or the app runs.

## Rules

```bash
seenot-desktop rules list                   # every rule in a sentence: on/off, active now, today's usage
seenot-desktop rules show social --json     # every field, plus exceptions learned from the pop-up
seenot-desktop rules starters               # the ready-made rules, and which you have

# A rule in your own words. With --minutes or --visits it's a budget; without, SeeNot steps in.
seenot-desktop rules add news --what "reading news: news sites, articles and headlines" \
    --minutes 15 --when "mon-fri 09:00-18:00" --site nytimes.com --on-purpose-ok
seenot-desktop rules add feeds --from-starter

# Change anything: key=value sets, key+=item / key-=item edit a list, key= removes a key.
seenot-desktop rules set news threshold=0.25 sites+=bbc.com 'what=news articles and headlines'
seenot-desktop rules set shortvideo 'when+=mon-fri 09:00-18:00'   # a creator: only during work hours
seenot-desktop rules off videos                                    # kept, but never asked
seenot-desktop rules on videos
seenot-desktop rules remove news
```

`what=` edits whichever description the model reads. The fields, as in
`rules.example.toml`:

| Field | Meaning | `rules add` flag |
|---|---|---|
| `description` | what the rule is about, in your words; the model reads it | `--what` |
| `kind` | `deny` (step in) or `time_cap` (count; step in over budget) | set by `--minutes` / `--visits` |
| `minutes_per_day`, `visits_per_day` | the budget | `--minutes`, `--visits` |
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
seenot-desktop rules test news              # asks the model this one rule on your last 100 distinct screens
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
which really are the rule, and let SeeNot pick the threshold:

```bash
seenot-desktop rules label news --yes 6d159df9 0f437a14 --no 52a39a64 c1d80582 d4f385bc
seenot-desktop rules tune news              # the threshold that's right on 90% of hits, from your answers
seenot-desktop rules tune news --apply      # write it to rules.toml
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
# Kinds of page no rule fires on, in your words
seenot-desktop allow list
seenot-desktop allow add games --what "a video game or game launcher" --app com.valvesoftware.steam
seenot-desktop allow set music sites+=music.amazon.com
seenot-desktop allow off shopping

# Things that look like a rule but are fine
seenot-desktop except list                  # typed ones, and those learned from "Not this one"
seenot-desktop except add videos "a lecture or conference talk"
seenot-desktop except remove shortvideo "How to fix a bike chain"   # the text, page title or URL

# Apps and sites where no rule ever fires ("Never here" on the pop-up)
seenot-desktop never list
seenot-desktop never add --app net.whatsapp.WhatsApp --name WhatsApp
seenot-desktop never remove --site example.com

# Settings
seenot-desktop settings show
seenot-desktop settings set budgets=true                  # count time caps; step in only over budget
seenot-desktop settings set no_monitor+=com.tencent.xinWeChat   # never read at all
seenot-desktop settings set allow_sites+=gitlab.com       # never judged; links from it count as on purpose
```

## Many changes at once

```bash
seenot-desktop config export > cfg.json          # settings, allow classes and rules, as written
# edit cfg.json
seenot-desktop config apply cfg.json --dry-run   # the diff
seenot-desktop config apply cfg.json             # all of it, checked together, or nothing
seenot-desktop config undo                       # changed your mind
```

Entries missing from the file are kept: a partial list never deletes
anything. `--prune` removes what's missing. Near the question limit, this
is how to swap rules: switching one off and adding another in separate
commands would be refused halfway.

## Is it running

```bash
seenot-desktop status            # app, model, config and question budget, what it judged last (exit 5 if down)
seenot-desktop config check      # does rules.toml load
seenot-desktop schema            # every field and its grammar
```

## For an agent

`.claude/skills/seenot/SKILL.md` is the operating guide for Claude Code:
start with `status`, `rules list` and `schema`, map the user's words onto
fields, dry-run, respect the question limit, and test a new rule before
switching it on. A user saying "stop me watching streams during work, but
lectures are fine" maps to:

```bash
seenot-desktop rules set livestream 'when+=mon-fri 09:00-18:00' allow_learning=true --json
```

What the commands don't do yet: pause or snooze the running app (the menu
and the pop-up do), and anything that needs the review page's screenshots.
