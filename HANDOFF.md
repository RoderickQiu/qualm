# Handoff: qualm

Written 2026-09-22; updated the same day after the trials, again after
the MVP was built, on the night of 2026-09-23 (pop-up, focus sessions,
dashboard, 8-bit model, `serve` / `doctor`), on the day of 2026-09-23
(check-ins replace daily budgets; then setup, Qualm.app and hosted Jev), and
late on the night of 2026-09-23 after an audit for a fresh user, early on
2026-09-24 after its verification round, and later that morning after the
third and last round (see "A fresh user's audit"), that night for releases and
updates (see "Releases and updates"), and late that night for About Qualm and what another
person's agent finds (see "About, the skill and the logs"). This is the working
document: update the Status and Measured sections as you go. README.md is
the public face and stays short: built on Kev and Jev, local-ready, what it
does, getting started. docs/MANUAL.md is the long version.

## What this is

Qualm is a macOS take on SeeNot, the Android app (it was called seenot-desktop
until 2026-09-23). SeeNot's model call is already a typed decision:
every 5 s the Android app asks a vision LLM for one `decision` per rule from a
fixed set, a `sensitive` flag, and a 0–100 `confidence`. That is the job Jev
(TypeSafe AI's "System One" model, released 2026-09-15) is built for. Jev
returns typed answers (choice, score, yes/no) with calibrated probabilities in
one parallel pass, in 70–500 ms. It is text-only, so the Mac version reads the
screen as text through the Accessibility API instead of taking screenshots.

The backend is anything that speaks TypeSafe's System One API:
- **Kev**, local (default): open-source Jev-style models on Qwen3.5, 0.8B/4B/9B,
  Apache-2.0. It runs through MLX on this Mac, and screen content stays on the
  machine.
- **Jev**, hosted: `[settings] backend = "jev"` (picked in setup;
  QUALM_BACKEND overrides). The key is in the login keychain (`keychain.py`;
  a checkout's git-ignored `.env` and the environment are read first). Its
  scores are shifted onto Kev's scale, so the same rules.toml works for both
  (Measured, "Hosted Jev"). Never a fallback when Kev is down.

## Status

Trials run 2026-09-22 (second session). Short version:

| Question | Answer |
|---|---|
| Can a local model make the decision? | **Yes, with Kev-4B.** Not with Kev-0.8B. |
| Does it work at the Gate's current thresholds (0.85 / 0.5)? | **No.** 4B ranks pages almost perfectly but its `p_hit` is low: stock pages never go above 0.43. Thresholds must be per rule and low (0.06-0.20), or the model recalibrated. |
| Chinese vs English rule text? | Both work on 4B. English is better for `social` (recall 0.90 vs 0.50 at precision >= 0.9). |
| Does the state fit Kev's 384-token training limit? | Yes: p50 176, p95 279 tokens at `--budget 700`; 2 of 119 captures go over. |
| Is 4B fast enough? | **~0.85 s** per reading warm (p95 1.1 s), 25 s first call. Fine for a 5 s loop; not the 70 ms of 0.8B. |
| Does 4B fit on this Mac? | Only with MLX's buffer cache capped (17 GB -> 9.5 GB). Uncapped, it swapped the machine and latency p95 hit 9 s. |
| Fine-tune on this Mac (MPS)? | **No.** 0.8B training used 20 GB and produced no step in 15 min (Qwen3.5 linear-attention kernels fall back to reference PyTorch). Use CUDA / Modal. |
| Is the label set real? | **No, it is a stand-in.** 119 pages opened by script and labelled by what was opened, not a day of your own use. See "Trial data". |

### Since the MVP (optimizations, 2026-09-22)

Each one addresses something seen in real use, or a finding from the
research on digital self-control tools:

- **Answer cache**: 92% of real model calls re-asked text the model had
  already read. Answers are cached by exactly what the model reads and is asked.
- **Ask only on change**: a new screen after 0.5 s; the same screen's changed
  text at most every 30 s; nothing changed, no call.
- **4 s before a pop-up**: pages passed through, or still loading, don't pop up.
- **Reasons in words** (`explain.py`): Kev returns probabilities only, so the
  reason replays the decision itself (since 2026-09-25): `policy.decide`
  puts its facts on each pop-up (`Decision.facts`: the list entry that
  matched, the feed signal, the page before, opened on purpose), and
  `explain.reason` says what fired, model first ("Kev is 95% sure, and
  youtube.com/shorts is on your list too"; "A close call: Kev gives it 34%,
  just over your line of 30%"), then one contrast: the model's own doubt
  ("It also read the page as getting something done"), how you got there
  ("from another page on youtube.com, not from a search") or "not a lecture
  or a tutorial". No model call; every clause only when its fact is true.
  Before, a site-list hit said only "This address is on your list" even at
  95%, and three rounds of blind viewers of the video read that as a plain
  blocklist. Of this Mac's 41 pop-ups since setup, 33 fired on the model
  alone, 4 on the feed signal, 4 on the list (the model agreeing each time).
  Plus which part of the screen carried it (asked again with only the
  title, and with only the page text). Time2Stop (CHI 2024) found
  explanations raised the accuracy and receptivity of interventions.
- **Friction on "I need it"**: unlocks after 5 s, doubling with each snooze in
  the last hour (to 60 s), and asks what for. In the one sec study (PNAS
  2023), the option to back out and a short wait reduced use; the message
  alone didn't.
- **Review queue, closest calls first.**
- **Idle** (`presence.py`): time caps stop while the screen is locked, or after
  2 min without input, unless the front app keeps the display awake (a video).
- **This week** tab: pop-ups per rule and day, your answers, time per budget.
- **`install`**: LaunchAgents for the model server and the app, restarted on crash.
  Not run yet: under launchd, macOS asks for Accessibility and Screen
  Recording for the Python binary itself.
- **Never judge Qualm**: a running copy had judged a demo panel, and the review
  page opened inside Cursor (a `vscode-file://` URL) had popped up as stocks.
  Qualm now skips its own panel and any window titled "Qualm review" (or, in
  logs from before the rename, "SeeNot review").
- **Work tools are left alone** whenever the model's best guess is "work", not
  only when it's 60% sure: code and notes are full of words any rule matches.
- **Stocks removed from the shipped rules**: a niche habit that, in real use,
  fired on shopping chats and on Qualm's own review page. The trial numbers
  below still include it.
- **[[allow]] classes, never here, thin screens, Chrome capture**: see below.
- **Personalizing is all commands** (`personalize.py`, `config.py`, `trial.py`;
  docs/PERSONALIZE.md): `rules`, `allow`, `except`, `never`, `settings`,
  each with `--json`, so a person or an agent can do everything from a
  terminal. Edits are surgical (comments survive), checked before saving,
  and the old file is kept as `rules.toml.bak` (since the audit: saved whole
  under a lock, the last 20 versions in `backups/`). New per-rule fields:
  `sites` (plain domains in place of regexes), `when` (hours and days),
  `enabled`, `note`. `rules test` scores a rule on your own recent
  screens through the live gate; `rules label` + `rules tune --apply` set
  its threshold from your answers. A new rule's default threshold is 0.2,
  not 0.5: at 0.5, Kev-4B's low scores meant a new rule would almost never fire.
  `rules.example.toml` is English only now, and the app creates rules.toml
  from it on first run.
- **A question limit, and a CLI an agent can drive safely.** Each reading may
  ask at most `max_questions` (25) questions: rules on at the same moment +
  allow classes + 3 shared, checked over the whole week (rules with
  non-overlapping `when` share a slot); a change past it is refused.
  Every command takes `--json` (errors too, with exit codes 2/3/4/5), every
  change `--dry-run`; `config export|apply|undo` changes many things in one
  checked step (`--prune` to remove); `schema`, `status`; `rules test --what`
  tries wording as a draft. `.claude/skills/qualm/SKILL.md` tells Claude
  Code how to use it.

### The night of 2026-09-23

From a day of real use (1,242 judgements) and a pass over the research on
these interventions (one sec PNAS 2023, Time2Stop and InteractOut CHI 2024,
Lyngs CHI 2019/2020, WellScreen CHI 2026):

- **Feeds must be clearly for entertainment.** `feed_hit` needs
  P(entertain) >= 0.6 (`ENTERTAIN_MIN`), not just the argmax. It had fired
  on an Apple Ads "recommendations" page at 0.41; every trial feed scores
  0.82+, and the trial simulation is unchanged.
- **Titles lose browser noise**: the browser's name, Chrome's "High memory
  usage - 1.2 GB" (on 89 judgements; its number changing looked like a new
  page and re-asked the model) and unread counts ("(3) Inbox").
- **The pop-up, redesigned** (`ui.py`, `app.py`): a dark blurred HUD over a
  soft dim of every screen; the rule as a phrase ("This looks like short
  videos made for endless swiping."), the page with its app icon, why, and
  a neutral context line ("2nd time today · last at 14:20"; "Your 10
  minutes for 'a recipe' are up"). Take me back is the default; I need it
  counts down, then asks what for; Not this one / Never here are quiet
  links; each answer ends with a short "got it". Nothing shames ("again?").
- **Focus sessions** (SeeNot's session intents): menu, dashboard or `qualm
  focus write the report --minutes 50`. Every rule steps in at once, time
  caps included, and the pop-up says "You're here to: write the report."
  `qualm pause 30` too. Both live in `data/session.json`, which the running
  app reloads, so menu, CLI, dashboard and agents share one state.
- **Dashboard** (`dashboard.html`, served by `review.py`): Today (focus,
  pause, the day's pop-ups and budgets), Review, All screens, Insights (how
  pop-ups ended per day, per week, hour x weekday heatmap, what you unlocked
  time for, focus sessions, budgets, how often your reviews said it was
  right, one question a week: was the time worth it?), Rules (on/off
  switches through `config.py`, thresholds, exceptions, never-here). No
  score, no streaks. Light and dark.
- **The dashboard only takes changes from itself**: POSTs need a JSON body,
  this Host and no foreign Origin. Before, any site open in the browser
  could post a text/plain form to 127.0.0.1:8765 and change thresholds.
- **8-bit Kev-4B by default** (see Measured), and **`qualm serve`** runs the
  server from this checkout in one command; `install` uses the same.
- **`qualm doctor`**: the Mac, memory, swap, Accessibility, rules.toml, the
  Kev repo, the model server (quantized or not), the app, start-at-login;
  the fix for each.
- **`qualm app --demo deny|feed|budget|focus|prompt`**, and
  `experiments/demo_data.py` (three made-up weeks, for screenshots: your own
  logs are personal and must not go in a README).

Verified: 65 tests; every panel state rendered and screenshotted (README
images); the dashboard's tabs in headless Chrome, light and dark, on a
copy of the real data and on demo data; its guard (text/plain, a foreign
Origin and a rebound Host get 403); rule on/off round-trips rules.toml
byte for byte; `qualm serve` loads 8-bit Kev and answers through the SDK
(0.58 s warm, 10 questions); focus/pause from the CLI reach a running
watcher.

Live end-to-end (new app, isolated data, 8-bit server, a test rule on
example.org so the running old app stayed out of it): Safari opened
example.org, the screen dimmed and the panel came up with the evidence
line from the model; Return = Take me back, and **Safari went back** to
example.com (first time verified; "back" logged). Second visit: I need it
→ "Unlock 10 min", a reason, Return → snoozed with the reason, Safari
stayed. The menu bar item opened (visible next to the old one), "Start a
focus session…" → the menu bar showed "50m"; `qualm focus --stop` and
`qualm pause 15` from the CLI reached the running app within seconds.
Not verified: a day of real use with the new panel; typing the reason
with a Chinese input method active (Return commits the composition first,
as it should, and the second Return unlocks).

Then an independent review of the whole diff; fixed from it: the
dashboard answered GETs for any Host (DNS rebinding could read your
screen log and screenshots); a new focus or pause didn't re-judge the page
you were on; a second focus session didn't close the first in the log; the
15 s refresh could wipe what you were typing; with both a minute and a
visit cap the pop-up could name the wrong one; a malformed session.json
could stop the watcher; session and usage files are now written
atomically. Also from the live test: while the model is down or timing
out (it hit the 30 s timeout under swap), a rule's own sites still step
in, as `sites` always promised.

### Check-ins instead of daily budgets (day of 2026-09-23)

Asked to leave testing mode for the "real" one, the user rejected the
daily budget itself, and they were right (docs/POLICY.md, "Check-ins, not
daily budgets"): it reads as an allowance, puts no friction on the first
44 minutes, and can't see the short check that becomes forty minutes. Now:

- `kind = "check_in"` replaces `time_cap`; `minutes_per_day`,
  `visits_per_day` and `[settings] budgets` are gone. New settings:
  `max_wait_s` (60) and `extensions` (1). An old file won't load and says
  to run `qualm config migrate` (done on this Mac's rules.toml).
- On arrival (not on purpose, not learning): "What are you here for?", a
  few words, 5/15/30 min (5 is Return). The session covers the rule on any
  site; nothing pops up until it's over. Time's up while you're there:
  Done (default, goes back) or "5 more" once after 10 s; then only a new
  check-in. The wait before Start: 0 for the day's first session, then 5,
  10, 20, 40, 60 s; doubled within 20 min of a session ending; +5 s for 15
  min, +10 s for 30.
- **The watcher now re-judges an unchanged page when the policy asks**
  (`Policy.rejudge_due`): when a session's time is up, when an "I need it"
  unlock runs out (before, sitting on the same page after 10 minutes never
  popped up again), and 30 s after "Take me back"/"Done", so an app with no
  Back steps in again.
- Sessions are logged (`decisions.jsonl`, `type: session`, start / extend
  / end with what for, said and stayed) and rebuilt from the log on
  restart. Dashboard: "Check-ins today" (running and finished sessions),
  "Checked in" as a pop-up outcome, and "what you said, and what you did".
- The model's question for these rules is word for word the old
  time-cap question, so thresholds and trial numbers still hold.
- **Take me back leaves the site** (found in real use on Chrome: one
  Cmd-[ on a Xiaohongshu note went to the feed, which popped up again; and
  the dimmer didn't take clicks, so browsing had gone on meanwhile and the
  keystroke hit another page). `watcher.leave()` goes back until the tab is
  off the pop-up's host or on a page judged fine (`Policy.page_fine`), else
  a new-tab page; Chromium through AppleScript (`go back`, `URL of active
  tab`), Safari by keystroke with the URL read back. Verified live in a
  scratch Chrome window: iana.org/domains → example.com in 2 s, two steps.
  The dim now takes clicks while a pop-up is open (`block_clicks`, default
  on, menu bar toggle; the user's call); the 5 s menu timer lifts a dim
  left without a panel, so a bug can't leave the screen unclickable.
  Also: the feeds pattern `xiaohongshu\.com/explore` matched every note
  (`/explore/<id>`); now only the explore page itself.
- **Take me back closes the window, not the app** (found in real use: a
  photo opened from a chat, in the chat app's separate viewer window,
  stayed open and the chats came to the front). Outside browsers,
  `watcher.close_window()` presses the close button of the window the
  pop-up was about (matched by title and frame) when the app has others
  open; with one window, the app is hidden as before. Verified live on two
  Finder windows; not yet on the chat app's viewer itself.
- **Chats aren't social media; rules change through an agent** (from real
  use: WeChat chats kept checking in as social, four "Not this one" didn't
  stop it, and hand-written rules backfired). Measured on 760 real
  screens: WeChat chats scored 0.15-0.41 on social (threshold
  0.30). An exception in words ("chatting in WeChat, WhatsApp or iMessage
  is fine") pushed WhatsApp from ~0.17 to ~0.50; an allow class `chat`
  scored chats 0.56-0.81 and social pages 0.16 or less, so it's a starter
  now. Three changes follow from it:
  (1) "Not this one" in a window with no address saves a score bar for
  (rule, app, title) at its score + 0.1 (`FINE_MARGIN`) instead of the
  title as words, which the model couldn't use ("Weixin");
  (2) `qualm allow test` scores an allow class like `rules test` does, and
  says which past pop-ups it would stop; `qualm guide` prints the agent
  guide (= the skill's body, kept equal by a test), with "measure, don't
  guess" and a workflow for wrong pop-ups;
  (3) menu bar "Change rules with your AI agent…" (`agent.py`): why, and a
  prompt to copy with the pop-ups marked wrong in the last 24 h; a second
  "Not this one" in one place points there; setup's last page says so.
  Screenshots checked off-screen (panel, confirm, setup light); the live
  app needs a restart for all but the `chat` class.

Verified: 75 tests (the session life cycle, the wait, the migration, and a
watcher test where only the re-check can say time's up; it caught a bug
where the session ended in the tick between noticing and asking). An
independent review then found, all fixed: a "time's up" dropped behind
another pop-up left the page unguarded; a slow answer let the session end
under the open pop-up; a session that ran out while Qualm was off counted
as just ended (doubling the next wait); two check-in rules on one page
asked twice; the dashboard counted extensions twice and broke against an
app still running the old code. Also:
`app --demo checkin|timesup` screenshotted (the countdown runs 20 → 0, then
"Start 5 min"); the dashboard's Today and Insights in headless Chrome on
demo data. Not verified: the live loop in a browser with the new code
(the user was at the Mac; the demo pop-ups already interrupted them), and a
day of real use.

### The afternoon pass (2026-09-23): sections 1-3 of "what's left"

The user: "for feed, low false positive is more important", then do the rest
of correctness, engineering and interventions autonomously. Two forks did
capture (state.py, ocr.py) and housekeeping (retention.py, uninstall,
keychain); the rest in the main thread.

- **Feeds, precision first**: in real use every feeds pop-up that fired on
  the score alone (0.35-0.40: a work chat's activity inbox, an AI chat's page,
  Qualm's old page) was wrong, and every right one came from a site pattern
  or feed_hit. Threshold 0.3 -> 0.5, plus weibo.com/ and m.weibo.cn/ as
  sites. Replaying the real log: 10 pop-ups on 9 pages -> 5 on 4, all
  genuine feeds. Trials: precision 1.00 for Kev and Jev, recall Kev
  0.60 -> 0.67, Jev 0.67. Applied to the user's rules.toml too.
- **"Sensitive" audited**: 125 skipped of 1,933 judgements: 51 lock screen,
  4 Touch ID, 69 Chrome, 1 other. The Chrome ones had no content logged; by
  the pages before and after: checkouts, a single sign-on page, a password
  manager, mail, ad consoles; none entertainment. Private pages now keep
  their site (host only) and score, never title, text or screenshot.
- **The shift on real screens**: 250 real screens (messaging
  and mail left out) re-asked to Jev: the same decision as Kev on all 250;
  none taken for private; page kind agrees 86%, purpose 78%; 0.19 s p50.
- **Capture** (fork): Safari's AutoFill popover no longer leaks into later
  pages (the walk drops what it read before the page, and skips menus and
  popovers outside it); Chromium's empty first read is retried once per
  process (0.3 s); **OCR** (Vision, accurate; zh-Hans + en-US then, the
  Mac's preferred languages since the audit) when a
  non-browser window has under 20 characters of AX text and no text area
  (so terminals, editors and notes are never pictured): ~70 ms warm.
  `ScreenState.ocr` marks it. Not verified live: a real AutoFill popover, a
  real Chromium first read.
- **Event-driven watching** (`events.py`): AX observers on the front app
  (focused/main window, title) and app activation wake the watcher; idle, it
  reads every 3 s instead of 0.5 s (settling screens and waiting pop-ups
  stay at 0.5 s). The app used ~2% CPU polling. Verified live: events
  arrive and follow app switches.
- **Retention** (fork): `keep_days` 90 (judgements, decisions; reviewed ones
  kept), `keep_shots_days` 30; run at start and daily; the dashboard shows
  "no screenshot" for pruned ones. **`qualm uninstall --all`** (`--dry-run`,
  `--yes`): login item, keychain key, shim, Application Support, logs; the
  Hugging Face downloads are listed with the command to remove them.
- **Keychain**: the key moved from .env (Qualm.app reads no .env).
- **Friction**: returning within 30 min after "Take me back" doubles the
  "I need it" wait per return (with the snooze doubling, capped by
  max_wait_s); in a focus session the first hit gets a corner nudge ("You're
  here to: ...", Take me back / Not now), no dim, keyboard not taken, and the
  full panel if still there 20 s later; the headline rotates between three
  wordings per kind (a tooltip says so); focus words never rotate.
- **The 8-bit copy published** on Hugging Face: RoderickQiu/kev-4b-mlx-8bit
  with head.pt, tokenizer, provenance.json (revisions, method, SHA-256),
  LICENSE, a model card (credits, changes, quality). kevserve fetches it when
  the provenance and hash match (QUALM_PREBUILT; localmodel.PREBUILT). A real
  first start from an empty cache: 191 s (download), a 4.8 GB peak, never
  bf16; the same answers as the running server (0.0 on 15 x 10). This copy
  equals quantizing at load exactly (0.0 on 119 trial pages) and is within
  0.012-0.038 of bf16.
- **The managed server, live**: the user's hand-started server stopped; the
  app started its own from kev-env: the first start (building the 8-bit
  copy, 4.2 GB) took 100 s and peaked at 16 GB; a later start from the copy
  11 s and 4.8 GB. It restarts a server that stops (theirs or its own)
  within ~5 s: verified by killing it. Readings 2-3 s after (was 3-20 s).
- **Qualm.app, live**: `open` like Finder: the process is
  Qualm.app/Contents/MacOS/Qualm, macOS names it "Qualm" (com.qualm.app,
  with its icon), the setup assistant shows; the user's app didn't judge it.
  Not done: granting it Accessibility (the user's click) and switching the
  daily app to it.

### Setup, Qualm.app, the local model managed (day of 2026-09-23)

Asked to make setup easier and more universal. Before: two repos, two
terminals, everything relative to the checkout, "16 GB" as the bar.

- **Per-user folder** (`paths.py`): rules, data, the model runtime and the
  8-bit weights in `~/Library/Application Support/Qualm` (QUALM_HOME
  overrides). A checkout's `rules.toml` in the current directory still wins
  until `qualm setup` copies it and `data/` over (copies; nothing moved or
  deleted); since the audit only when that directory is a Qualm checkout. `rules.example.toml` and the server script (`kevserve.py`, was
  experiments/serve_capped.py) moved into the package; the root
  rules.example.toml is a symlink.
- **Kev as a managed dependency** (`localmodel.py`): its runtime (Kev pinned
  to commit 08ab0b8, PyTorch, MLX; 1.0 GB) goes into `kev-env/` via uv, on
  first use (since the audit from a GitHub archive of that commit: no git). The app starts the server when nothing answers on :8009 and
  stops it on Quit; `qualm serve` still runs it in a terminal
  (`--kev-dir` for a Kev checkout). No second clone, no second terminal;
  `install` is one LaunchAgent (the app), and removes the old com.qualm.kev.
- **8-bit weights saved once** (`kevserve.py`, QUALM_MODEL_CACHE): the first
  start loads bf16, merges, quantizes and writes `models/<ckpt>-<base>-q8g64/`;
  later starts load that and never hold bf16. Verified on Kev-0.8B: answers
  from the saved weights are identical to the freshly quantized ones (max
  |dp| 0 on 15 pages x 11 questions), from both the dev Kev env and the
  installed runtime. Found and fixed: Kev sizes the pointer head from the
  embedding's width, which is packed on quantized weights (1024 -> 256).
  Kev-4B's cache is built on its next start (not done here: the live
  server was running).
- **Memory in numbers, not "16 GB"**: the server holds 6.1-7.1 GB
  (measured); setup compares that with what's free now, counting swapped-out
  pages as in use. On a 24 GB Mac already deep in swap: "not enough: it
  would swap", hosted recommended.
- **`qualm setup`** (and flags for scripts: `--backend`, `--key`, `--rules`,
  `--login/--no-login`, `--permission`), and the **setup window**
  (`onboard.py`), both through `setup.apply`: where the model runs (cards
  with the costs; key checked against Jev before saving to the keychain),
  Accessibility (live status), the starter rules (switches), open at login.
  The window is a 5-page assistant built with Auto Layout; the first
  hand-placed version overlapped and was redone. Screenshots of every page,
  light and dark, with and without a key and permission, and the whole flow
  driven in code in a throwaway home.
- **Qualm.app** (`packaging/`, `uv run python packaging/build_app.py`):
  149 MB, DMG 90 MB, ad-hoc signed, not notarized. A C launcher embeds the
  bundled CPython, so the process *is* Qualm.app (Accessibility asked for
  "Qualm", bundle id com.qualm.app); with arguments it acts as `python`,
  and setup can add a `qualm` shim to ~/.local/bin. The app carries uv to
  install the local runtime. Verified without opening it: imports, bundle
  id, `-m qualm doctor`, `codesign --verify --deep --strict`. Not verified:
  opening it from Finder, and which name the permission prompts show.
- **`doctor`** checks what the chosen model needs (runtime, memory, server;
  or the key) instead of a Kev clone.
- **Never judge Qualm's own windows**: while I rendered the setup window,
  the user's running (old) app judged it ("Short videos: Douyin, TikTok,
  YouTube Shorts") and popped up as short video (judgement at 14:25:16 in
  data/). The watcher now skips "Set up Qualm" and anything from
  com.qualm.app, with a test. Also from testing: a flow test run from the
  checkout wrote the user's own rules.toml (legacy wins); restored by hand
  the same minute (both lines removed, .bak reset), and `setup.apply` now
  only ever writes the per-user copy.

- **Menu bar toggles**: *Model* (On this Mac / Hosted, ticked; hosted
  clickable once a key is saved; the last reading's time, to compare) switches
  `[settings] backend`, judges the current screen again, and stops a server
  the app started when going hosted (one from `qualm serve` is left alone).
  *Watch for* turns each rule on or off (read from rules.toml, so rules that
  are off stay listed). *Open at login* (from Qualm.app only until late
  2026-09-24; from a checkout too since). The watcher's
  answer cache is keyed by backend too, so a switch really asks the other
  model. Tested by building the real menu in a throwaway home and clicking
  through (hosted, a rule off, back, on).
- **Jev's cost, from real use** ($0.042 per million input tokens, output
  free; early-access pricing): a call is ~1,500 input tokens (5 rules, 2
  allow classes); the two logged days made 339 and 877 model calls:
  ~$0.02-0.06 a day, ~$0.50-1.25 a month. Each rule adds
  ~127 Jev tokens (measured). All of it, with Kev's memory, in docs/MODELS.md.

93 tests.

### A fresh user's audit (night of 2026-09-23; rounds 2 and 3 on 2026-09-24)

How it was run, step by step (which agents, how they worked together, what
each step produced, why it took all night), written for the user:
docs/AUDIT-2026-09-24.md.

Asked to make Qualm work for someone who isn't its author: another Mac,
maybe neither Chinese- nor English-speaking, through the app (setup window,
menu bar, pop-ups, dashboard) or through Claude Code driving the CLI.

**How it was run, strictly in the background** (the user was at the Mac
with their own Qualm live): every run in a throwaway QUALM_HOME and folder;
no window ever ordered in (the menu, panels and setup window built
off-screen, activation policy 2, rendered to PNG); no keystrokes,
AppleScript or screen captures; the live app and its model server never
touched; model calls capped and spaced (2 s or more apart on the shared
server); the keychain, LaunchAgents and the `qualm` shim only through
monkeypatched functions. Ten lenses (install and first run, Claude Code on
day 0 and in week 1, the setup window and app, the menu and pop-ups, the
dashboard, generalization in the code and in the model, robustness, the
docs' claims), then three passes on what they couldn't reach: a running day
end to end against a stub model, real headless Claude Code sessions, and the
local model's first start against a fake Hugging Face. 204 findings, each
re-checked by a second, skeptical agent: 149 confirmed, 52 partly (often a
corrected cause or severity), 3 refuted; 2 critical, 31 high. Then seven
fix branches in parallel, one per area, one integration pass, and this pass
(starter rules and docs). Each branch's commit says what it fixed; the
short version:

- **rules.toml** (`config.py`, `rules.py`, new `jsonl.py`). The first
  critical: agents run commands in parallel, and parallel edits lost changes
  and could leave rules.toml with one rule and no settings. Every change now
  reads, edits and saves under a lock (`rules.toml.lock`) and replaces the
  file whole; `config undo` steps back through the last 20 saved versions
  (`backups/`). Values are type-checked on load and on save (`no_monitor`
  saved as a string meant those apps were read after all, and a bad
  `allow_urls` regex killed the watcher); a broken file names itself, the
  line and the fix; a BOM or CRLF
  is fine; `config apply` keeps what it leaves out (null resets a key,
  `--prune` removes); sites match in any case or script; `when` takes several
  hours; a torn line in a data log is skipped.
- **Policy and capture** (`policy.py`, `watcher.py`, `state.py`). Take me back
  closed the whole window in Opera, Vivaldi, Chrome's other channels, Firefox
  and others; now Chromium browsers go back through AppleScript, Gecko and
  WebKit ones by keys with the address read back, and a browser window is
  never closed. "Not this one" no longer adds page titles to what the model
  reads (two took YouTube's home page from 0.20 to 0.34 on social). "Opened
  on purpose" means a search or a link followed, not Cmd-Tab, and never on a
  rule's own sites. New rule field `apps`; app lists match names; password
  managers are never read; no screenshot without a reading that found the
  page not private; an error in one pass no longer stops the watcher; OCR
  reads the Mac's languages; Chrome's memory label goes in 52 of its 55
  languages; nothing is judged while the screen is locked.
- **Testing and tuning rules** (`trial.py`). `rules test` said "0 would
  count" for every check-in rule; hosted Jev's scores went unshifted into
  test and tune, so a tuned threshold stopped the rule firing live; tune
  could suggest a threshold worse than the current one; a user's deny rule
  never fired on a news front page or a store (content target plus the feed
  drop), and a shopping rule never on a store (the shopping allow class).
  Now `rules test` shows what the app would do and why, CLI and dashboard
  tune with one function that never does worse on your answers
  (`keep_current`), `rules add` judges the whole page and lets one item
  opened on purpose through, `overrides_allow` lets a rule step in on an
  allow class it names, and `except add --from`, `review --json --since
  --misses` and `no_screens` (not "is the model running?") exist.
- **Setup, status, doctor, the CLI** (`cli.py`, `setup.py`, `doctor.py`,
  `personalize.py`). The setup window vanished after the first `qualm
  status` (it keyed on rules.toml existing); hosted users always got "model
  unreachable" from status; setup's default login item plus the README's
  `qualm app` ran two copies; a scripted setup removed the login item. Now:
  the `.setup-done` mark; status and doctor know the backend (a free call
  checks the key) and find the app by `/api/hello`; setup never turns login
  on without a terminal or `--login`, keeps the saved model on a re-run,
  takes the key on stdin and refuses the disk image; a custom QUALM_HOME
  leaves the main install alone; the dashboard moves off a taken port;
  `--json` everywhere; `pause 2d` / `--until`; a focus session ends a pause;
  hints a stock shell can run.
- **The local model's first start** (`localmodel.py`, `kevserve.py`). The
  second critical: Kev's checkpoint wasn't pinned and upstream had moved, so
  every fresh first start missed the published copy and built bf16 (8.7 GB
  more, a 16 GB peak). Now Kev-4B, its base and the copy are pinned; a local
  build runs only when the copy is gone and the Mac has room; a failed
  server backs off (it restarted every ~5 s and each start downloaded from
  byte 0); the download resumes; no git; one start at a time; failures in
  words; the port moves if 8009 is taken; `hf_endpoint`; macOS 14 checked.
- **Menu bar, pop-ups, setup window** (`app.py`, `onboard.py`, `ui.py`,
  `explain.py`). Missing Accessibility and hosted trouble (key refused,
  quota, TypeSafe down) showed as a menu line that got overwritten; now a
  badge and a first line until fixed, the fix one click away, and one corner
  notice. One copy per Qualm folder (`app.lock`). A key window from Model.
  Answers of 2 characters (1 CJK) count; keys are ignored for 0.6 s; rules
  go by name, and a user's own rule is quoted. The setup window asks for
  Screen Recording (optional) and no longer claims no screenshots.
- **Dashboard** (`review.py`, `dashboard.html`). It told hosted users their
  screen text stays on the Mac; now it follows the backend. A status chip;
  pages sent in pieces (36,000 judgements: 131 KB in 0.3 s, where it sent
  the whole 89 MB log in 2.5 s); a broken rules.toml shows a banner, not
  blank tabs; exceptions go to rules.toml; `qualm week`.
- **Starter rules** (this pass). The video rule's "on Bilibili or YouTube"
  scoped it to two sites: Netflix, Prime Video and Vimeo scored 0.01-0.15.
  Measured through the real path (capture-shaped state → precheck →
  `decide.ask` → `Policy.decide`) on the audit's 53 made-up pages, 20 more
  (streaming services, Western feeds, Kick and Twitch, videos that aren't
  entertainment) and the 119 trial pages, old and new wording in the same
  call (each question is its own branch on Kev, so its answer doesn't
  depend on the others): hosted Jev 291 calls, local Kev 40 on the pages
  nearest a threshold. Kept: `videos` = "watching entertainment videos:
  comedy, gaming, variety shows, film clips, TV series and films" (made-up
  entertainment videos that check in: Kev 3 → 8 of 9, Jev 2 → 9 of 10;
  lectures, talks, tutorials, a work video and a meeting stay at 0.12 or
  less on Kev); `social` names Instagram and Facebook first (their pages
  0.47-0.58 → 0.83-0.89 on Kev); `livestream` names Twitch, Kick and
  YouTube Live first (no page changed side). Sites for Western home feeds
  (Facebook, Instagram, Threads, Reddit, m.youtube.com, Facebook Watch) and
  Facebook Reels; the Twitch pattern takes channels with a query string and
  skips Twitch's own pages (/downloads, /jobs, /videos/…); a Kick pattern;
  sec.gov out of `allow_sites`; a note on coding streams. The trial set:
  the same pages pop up (all 119 on Jev, the 11 nearest a threshold on Kev).
  Made-up pages: 25 of the 26 that should step in do on Jev (was 18), 16 of
  17 on Kev (was 10); of the hard negatives only the Twitch coding stream
  pops up (the false pop-up on twitch.tv/downloads is gone); a Netflix
  nature documentary now gets a check-in on Kev (either answer counted as
  right).
- **Docs** (this pass): README (Apple silicon only, Open Anyway, what hosted
  needs and costs, Screen Recording and Automation, mirrors, permissions and
  PATH from source, English-only interface, numbers off the trial set), the
  guide and skill (a first day with nothing to score, undo, `screens`, small
  `--last`, why / missed / week / `pause --until`, setup flags, what reaches
  the agent's provider; a test checks every command it names exists),
  PERSONALIZE, POLICY (Take me back per browser, opened on purpose, Not this
  one, private pages), MODELS (where Kev and Jev disagree, the first start),
  packaging. The menu's agent prompt says what to do when there's nothing
  to score yet.

**Decisions taken** (all three rounds; the ones that change behaviour; each commit has the rest):

- Changes to rules.toml are serialized by a lock and whole-file saves. Undo
  is 20 versions, one per saved change, rules.toml only, no redo; after a
  hand edit (broken or not) the first undo restores the version Qualm last
  saved (`backups/rules.toml.saved`) and drops only the hand edit (round 2).
  A `.bak` from before `backups/` becomes `backups/rules.toml.1`, the oldest
  version, on the first new save. Deleting rules.toml and letting Qualm make
  it again from the starters can be undone. A change that would leave an
  empty file keeps a `[settings]` line. Unknown tables and keys are
  refused; an `overrides_allow` naming no allow class loads (ignored), but a
  change that makes one is refused (round 2). A rules.toml with no record
  of the last save (an older Qualm's) is recorded as the last save the
  first time a command or the app finds it loading (not in a dry run; a
  busy lock leaves it to the next command). Every undo keeps the file it
  replaces as `backups/rules.toml.replaced` (the next undo overwrites it: a
  one-step redo by hand), and a kept version it passes over goes to
  `backups/NAME.aside` (`.aside2`, … when taken), which nothing reads.
  `undid` stays `hand_edit` or `saved_change`; a `note` says when there was
  no record of the last save. `config.undo_to` is a dry run of undo, so the
  load error, the menu, doctor and the dashboard offer undo only when it
  would work, and say where it goes (round 3).
- Take me back takes the browser path only in the browsers in Qualm's
  table (`state.BROWSERS`), whatever the page: every other app, web apps
  and Electron apps included, gets its window closed or is hidden (round 2
  reversed round 1's "an app Qualm doesn't know counts as a browser when it
  shows an http(s) address"). Keys are pressed only while the browser is
  in front; a Chromium browser that refuses AppleScript gets keys. Chrome's
  memory label is stripped only from Chromium browsers' titles.
  Screenshots only with a real reading. Round 3: a browser missing from the
  table whose window has an http(s) page and, outside it, a tab strip or an
  address field showing its site (`state.browser_window`) gets one Cmd-[
  while it's in front, never Cmd-T (in an unknown app it could be Show
  Fonts) and no 12-step walk; nothing to go back to counts as "stayed".
  Chrome and Edge web apps (`.app.` in the bundle id) and web apps whose
  tabs are inside the page are still closed or hidden. The table gained
  Dia, Comet, ChatGPT Atlas, Yandex, Whale, QQ Browser and ego lite
  (Chromium) and SigmaOS (keys); Atlas's bundle id is unverified.
- A rule's `apps` make it a hit there without asking the model about it;
  the other rules are asked as in any app (round 2). A rule that lists an
  allow class in `overrides_allow` steps in on it, and one that names the
  class's very app, or a listed site as narrow as the class's or narrower
  (music.youtube.com, not youtube.com), steps in on that place
  (`policy.claims`), and the model is asked there. A rule on a whole
  domain, a regex pattern or a browser app leaves the class's listed places
  to the class, as before round 2 (round 3; round 2 had let youtube.com
  step in on YouTube Music). The evidence check skips a pop-up from a
  rule's own app or site.
- The AX walk reads the page with its own budget, and looks inside the
  browser's own web areas (side panels, Vivaldi) only for 500 nodes, only
  when no page was found outside them. A tab strip, and anything over 30
  items in the browser's own web area, is walked last and only until the
  page turns up, and a tab is never walked into (round 3; Chrome with 300
  tabs: 2,443 AX calls → 31); windows with no page keep their reading
  order. An error in the watch loop of a new type and place prints its
  traceback at once; after that, a repeat that differs from the last one
  printed waits at least a minute (round 3). The login item's log
  (`~/Library/Logs/Qualm/com.qualm.app.log`) is set aside as `.log.1` past
  2 MB.
- Search results (round 3): a page read as search results is let through
  ("search results") for content and check-in rules, after the work-tool
  check; a deny rule with `target = "page"` and a rule's own sites still
  step in, and `review --misses` counts a flagged one as a miss.
- Tune is precision first and never worse than the current threshold on
  your answers; it leaves out answers from outside a rule's `when`, and
  counts answers logged before wording keys existed as the current wording
  unless the rule was reworded since (round 2). New rules:
  `target = "page"` (deny rules), `allow_intentional` on, `overrides_allow`
  from the rule's own English words, only when neither description has a
  word anywhere that says what the rule isn't about (not, no, but, except,
  other than, without, unless, instead, rather, fine, non-, any n't word,
  不, 除了, 除外, 以外, 之外; not 非 or 无, which sit inside common words);
  otherwise the named classes come back as `may_cancel` with their
  commands, so a false alarm only reports a class (round 3). `review`
  shows the newest 30, or with `--since` the newest 100 (`matching` stays
  the total, `truncated` says it cut); `except remove` matches an address,
  then words, then a title, and refuses a title that pages with different
  addresses share; an older removal record with an address takes only that
  address (round 3).
- Setup is "needed" until `.setup-done`, `judgements.jsonl` (an app or
  `watch` ran), a chosen backend, or a rules.toml an earlier version saved
  (`rules.toml.bak` without `backups/`); `decisions.jsonl` no longer counts
  (round 2). `.setup-done` is written when `backups/` is first made next to
  an older Qualm's `.bak`, so such a home stays set up after its first
  change (round 3). It never turns login on unless asked; from a checkout
  the question, and the setup window's box (round 3), default to no. A
  custom QUALM_HOME leaves the main install's login item, shim, key and
  logs alone, and every hint there starts with `QUALM_HOME=…` (round 3).
- A planned pause is `pause_later {from, until}` in session.json, beside
  `paused_until`, so an older app ignores it rather than pausing at once;
  Resume ends only the pause running now, `pause --stop` cancels both.
  `--from` is always its next occurrence and `--until` the next one after
  it, never the past: the weekend recipe run during a weekend plans the
  next one, and a warning names `pause --until '<until>'` for the one
  under way; a `--from` in the last 5 minutes counts as now (round 3). A
  timestamp no clock can show (milliseconds) counts as not set.
- Kev-4B, its base and the 8-bit copy are pinned (bump `MODEL`, `PREBUILT`,
  `PREBUILT_SHA256` and `PREBUILT_BYTES` together); building here needs
  16 GB of memory and 14 GB of disk to spare. Every model call finds the
  server the same way (`KEV_URL`, `models/server.json`, :8009), nothing is
  sent to another app on 8009, and no second model ever loads (round 2).
  A local listener that doesn't answer in time and isn't Kev gets a second
  look, three times as long (up to ~12 s on the app's main thread at its
  start), rather than a second model beside a busy tunnel. A mirror that
  serves the wrong file isn't tried again until hf_endpoint/HF_ENDPOINT
  changes; without a mirror a wrong file keeps the usual backoff (round 3).
- Hosted trouble: one notice per kind per run. One app per Qualm folder,
  also against an older copy that takes no lock (round 2). A rules.toml
  that doesn't load never stops the app: it starts on the version Qualm
  last saved, else the newest kept version that loads, else the starters,
  with the file's backend line (round 2). Nor does one it can't read;
  meanwhile the apps the broken file lists in `no_monitor` (read leniently)
  are never read, and nothing goes to the hosted model until the file
  loads, since it may name private apps in ways a scan can't catch; the
  rules' own sites and apps still step in, and the local model keeps
  judging (round 3). On a rule's own sites and apps the quiet links wait
  like "I need it".
- The dashboard looks at the last 14 days, with older on request. It serves
  the dashboard.html it started with; a tab from before an update reloads
  itself, and changes from a page without its version are refused. A focus
  nudge answered "Not now" with no panel after it counts as going back
  (round 3; round 2 counted it as I need it).
- Starter wording: "TV series and films" stayed in the video rule because
  without it Jev's weakest trial clip fell from 0.45 to 0.16 (with it 0.29,
  still over 0.25); "on any site" would have scored as well but made the
  check-in read "This looks like watching entertainment videos on any site."
  LinkedIn's feed and youtube.com/live stay with the model (a job search, a
  live launch event); the Chinese sites stay in the lists.
- The agent prompt (one sentence in `agent.py`), two dashboard sentences
  about saved versions and `onboard.py`'s docstring were changed in this
  pass, outside its files.
- Round 2's starters: Facebook's and Instagram's home pages are left to the
  model, since logged out they're login forms and a site steps in while the
  model can't answer; the Kick pattern leaves out Kick's own pages, as its
  sitemap and scripts list them on 2026-09-24.

Round 1 verified: 288 tests, from the checkout and from a temp folder
(they no longer need the checkout as cwd); the setup window's rules page
rendered off-screen with the new wording; the guide's commands, flags and
JSON keys run in a throwaway home.

**Round 2: verification, then fixes** (early on 2026-09-24). Every finding's
repro was run again on the end of round 1 (4dfd1a1 on the branch
`fresh-user-audit-history`), in the same strictly-background way,
plus four end-to-end passes: real headless Claude Code sessions, the GUI
off-screen, a user upgrading from main with the old app still running, and
a review of the whole diff. Of the original findings: **216 fixed, 46 partly,
5 declined** by design (a new rule's first-guess threshold, no rule editor,
the Twitch pattern on coding streams, and the like), **3 couldn't be tested**
there (a real agent session, the Qualm.app shim, a login item to remove),
and **2 not fixed** (a second copy next to an old app that takes no lock;
the app exiting on a rules.toml that doesn't load). It also found **109 new
or leftover problems**: 2 high, 27 medium, 80 low; 15 regressions from
round 1, 17 incomplete fixes, 58 new, 9 old ones still there and 9 in the
docs. Then eight fix branches (the same seven areas, plus starter rules and
docs), one integration pass, and this docs pass. What round 2 fixed:

- **rules.toml**: undo after a hand edit goes back to the version Qualm
  last saved and no further, which is what every load error promised
  (round 1's undo also threw away the last saved change); kept versions
  that no longer load are passed over and named; a `.bak` from before
  `backups/` is kept as the oldest version; removing the last entry keeps a
  `[settings]` line; a record appended after a torn log line is no longer
  lost with it (`jsonl.appending`); a site in another script matches its
  IDNA 2008 form, which browsers send (straße.de → xn--strae-oqa.de), as
  well as the 2003 one.
- **Policy and capture**: a rule's `apps` no longer silence the other rules
  in that app (a check-in naming Safari had turned off feeds there); a rule
  naming Spotify fires there; Take me back presses keys only while the
  browser is in front (it pressed up to 13 in whatever app was) and no
  longer presses Back in web apps, where it stayed; "It's part of the task"
  clicked after the session saves nothing (it saved a permanent
  exception); the page is read past a big side panel (30 AX calls, where
  it came back empty after 3,000); Firefox without a web tree is read from
  a picture again; OCR keeps Chinese first, so a German or Korean Mac reads
  mixed Chinese lines; the memory-label strip leaves Safari's "- 256 GB"
  alone; an error prints once a minute at most (one printed 17,000
  tracebacks a day); app lists set from the CLI resolve names everywhere.
- **Testing and tuning**: tune leaves out, and counts, answers from outside
  a rule's hours, and keeps pre-upgrade answers (one small `rules test`
  dropped them all); "videos, but not music or chat" no longer overrides
  chat; `can_be_cancelled_by` lists the allow classes an agent should
  judge, in any language; an `overrides_allow` naming a deleted class no
  longer breaks rules.toml (its fix couldn't run); a `rules test` cut short
  by the model returns what it scored (exit 5, `complete: false`); `except
  add --from` works on an app rule's pop-up; `review` says when it cuts
  (`matching`, `shown`, `note`), takes `--id` and plain dates, and shows how
  each pop-up ended; `--misses` leaves out what you chose to let through;
  usage.json is read leniently; judgements log their pop-up's decision id.
- **CLI**: `pause --from 'sat 00:00' --until 'mon 09:00'` (the guide's
  weekend recipe paused the working days too); one clock for end times
  ("Mon 09:00", not "09:00 tomorrow"); `config apply` refuses jev without a
  key; status says TypeSafe refused the key; a home made by the pre-fix
  setup isn't taken for a first run; a custom QUALM_HOME no longer takes an
  old app for its own, nor takes over another home's login item; `rules
  list --json` counts the pages let through with Not this one instead of
  sending their titles and addresses.
- **Local model**: terminal commands and the pop-up's explanation ask the
  app's server wherever it moved and never send a screen to another app on
  8009; a silent listener isn't taken for a model, a busy one counts as up;
  hosted chosen from the CLI stops the local server; `qualm serve` and
  kevserve never load a second model; one runtime install at a time;
  `hf_endpoint` must be https, and the prebuilt's SHA-256 and size are
  pinned; pruning removes only Qualm's copies; `QUALM_KEV_ARCHIVE` where
  GitHub is blocked; setup refuses the local model on macOS 13.
- **Menu bar, pop-ups, setup window**: a rules.toml that doesn't load no
  longer stops the app (the login item restarted it forever); an old copy
  that takes no lock is found; the key window's Cancel drops the check on
  its way; "No TypeSafe key is saved"; text measured by width (a Chinese
  focus intent showed as "You're here to…"); the quiet links wait on a
  rule's own apps; "Never on …" keeps
  its whole domain; rules in other scripts go by their words; an arrow icon
  while the model downloads, an hourglass while it loads; "Not this one"
  counted per site for the agent hint; the setup window opened from the
  disk image says so, greys out the local card where it can't run, and says
  6 GB.
- **Dashboard**: fits at 800 px; patterns said in words; the menu's words
  for Accessibility (picked up without a restart) and hosted trouble; a
  second watcher quitting no longer makes it say "Not running"; a tab from
  before an update reloads itself instead of saving clicks it never shows;
  a focus nudge and its panel are one pop-up, "Not now" counts as I need
  it (round 3: as going back, when no panel follows).
- **Integration**: the app starts on `backups/rules.toml.saved` first; the
  dashboard has the "no key" state, offers the local model only where it
  runs, and names rules in other letters as the menu does; the managed
  server's port search stops at 65535.
- **Starter rules and docs** (this pass): Facebook's and Instagram's home
  pages are out of the feeds sites. Logged out they're login forms, and a
  site steps in while the model can't answer, so a first start (a 6 GB
  download) put a feeds pop-up over a password field. With the model up
  nothing changes on round 1's recorded readings: Facebook's home is still
  feeds on both models, Instagram's is feeds on Jev and a short-video
  pop-up first on Kev, as before. The Kick pattern leaves out Kick's own
  pages (/login, /events, /drops, /advertising-policy, from its sitemap and
  scripts); other single names there really are channels (kick.com/signup
  is a channel called signup). Comments sit above the arrays, so edits keep
  them. `experiments/demo_data.py` writes the exceptions its Not this one
  and Never here answers make (agents told demo users their answer was
  lost). `status --json` says `app_older` when the running app is an older
  copy. The docs: this switch-over (step 0 sent you to a pre-audit
  dist/Qualm.app), the guide's first day (only the sites the user named:
  a German session had added 15 news sites, T-Online's webmail with them),
  weekend pauses, review ids, allow-class overlaps in any language, the
  model-server lookup, undo in two steps.

Round 2 verified: 362 tests, from the checkout and from a temp folder;
the starter changes on Rule objects and through `Policy.decide` with no
reading and with round 1's recorded readings (no model call this pass);
Kick's paths by the titles kick.com serves; the guide's commands in a
throwaway home.

**Round 3: verification, then the last fixes** (2026-09-24). Round 2's
fixes were verified the same way, in nine lenses (config, policy, trial,
CLI, local model, GUI, dashboard, docs, a fresh user end to end), each
repro run again on the end of round 2 (35bd489) against round 1's. Of the 151 findings
rechecked: **130 fixed, 19 partly, 1 declined** by design (`config undo`
leaves `never add` alone) and **1 not fixed** (the setup window ticked
"Open at login" from a checkout). It found **48 new problems**, none
critical or high: 11 medium, 37 low. Then four fix branches (core: config
and the CLI; policy and capture; testing and tuning; surface: the app,
dashboard and local model), one integration pass and this docs pass. Round
3 fixed 46 of the 48 (the two left are under Still open) and most of what
was still partly fixed:

- **rules.toml** (core). On a home laid out by main (rules.toml and .bak,
  no backups/: the user's), the first `config undo` after a broken hand
  edit also dropped the last saved change, which the error said it
  wouldn't. The first command, or the app's start, now records such a
  rules.toml as the last save; broken before that, the error and undo say
  where undo goes, and `note` says how to keep the change. Undo keeps the
  file it replaces (`backups/rules.toml.replaced`) and sets aside, rather
  than deletes, a kept version it passes over (`.aside`); `--dry-run` shows
  `undid`, `skipped` and both files. Load errors, doctor, the menu and the
  dashboard offer undo only when it would work. `never add --site 'not a
  site'`, `rules tune --precision 5` and a DEL character in rules.toml are
  refused in plain words.
- **CLI** (core). `pause --from X --until Y` paused from now whenever X
  was past ("next week off" asked on a Thursday paused Thursday and
  Friday): `--from` is its next occurrence, a warning names the command
  for one under way, and the weekend recipe on a Monday before 9 plans
  the weekend. A millisecond timestamp in session.json counts as not set,
  so pause and focus mend it; the menu's status line says when a planned
  pause begins; a home set up by main stays set up after its first change
  (`.setup-done`); `rules list --pages` lists the pages in text; `status
  --json` has an error whenever it exits non-zero; hints on a custom
  QUALM_HOME start with it.
- **Policy and capture** (policy). Take me back closed the whole window,
  every tab, of a browser missing from the table (Dia, Comet, Atlas,
  Yandex, Whale…): eight more are in it now, and one still missing is known
  by its window and gets one Back. Vivaldi past ~120 tabs and Chrome past
  ~400 sent tab titles to the model instead of the page: tab strips and
  long lists are walked last. A rule on youtube.com, qq.com or 163.com
  popped up on YouTube Music, QQ Music and NetEase Cloud Music: only a
  rule naming that very place steps in on an allow class's own sites.
  Search results wait for what's opened from them, for content and
  check-in rules, as POLICY.md always said (no pop-up changes on four
  recorded runs of the 119 trial pages). A pop-up from a rule's own app or
  site skips the "which part of the screen" check (two model calls, and a
  line that explained nothing); "It's part of the task" after the session
  ended says nothing changed; a new error prints at once.
- **Testing and tuning** (trial). "Videos other than music or chat" set
  `overrides_allow` and checked in on a WeChat chat (real Kev): an exclusion
  word anywhere now sets nothing, and the classes come back as `may_cancel`.
  With the model down, `rules test` said it "stopped answering after N of M"
  over old scores: now "didn't answer", and check `status`. `review --json
  --since today` returned ~886 KB for a real day: now the newest 100, with
  `truncated`. `except remove` of one page took every page with its title:
  now only that address. The dashboard's tuning sees rewordings in
  unanswered judgements; an evening rule's daytime `rules test` asks for no
  answers tune would drop; tune with too few answers says so
  (`enough_answers`); an allow class can't take a rule's id, and says why.
- **App, dashboard, local model** (surface). Starting on a broken
  rules.toml dropped the `no_monitor` apps the breaking edit added, and a
  hosted user's screens went on to TypeSafe: those apps are read leniently
  and never read, and nothing goes to the hosted model until the file
  loads. The broken-file notice goes when it's fixed, an unreadable file no
  longer stops the app, and Watch for keeps the rules that are off. The
  dashboard's banner says what the running app judges with, its commands
  run as this Mac's terminal does, "Not now" on a nudge with no panel
  after it counts as going back, and a pattern naming no site plainly is
  shown as written. The pop-up cuts a long title, never the site. A busy
  model behind an ssh tunnel or Docker is used, not moved from; a mirror
  serving the wrong file is named and not tried again until it changes;
  status gives the same download size as setup (6 GB on a first start),
  and doctor's reason when nothing answers, a KEV_URL elsewhere included.
  Model > "Show the local model's log"; the setup window leaves login
  unticked from a checkout.
- **Integration**: `config.undo_to` is a dry run of undo, so every place
  that offers undo says where it goes (the load error, the menu, doctor,
  the dashboard); the dashboard's banner uses those words and what the app
  starts on; the rules card counts answers from outside a rule's hours;
  the new hints run qualm as this terminal does.
- **Docs** (this pass): the guide and skill (the weekend recipe by day,
  undo's new keys, status errors and `model.why`, allow classes and whole
  domains, search results, `except remove` by address, QUALM_HOME in
  hints); PERSONALIZE (backups, the broken file, exclusion words and
  `may_cancel`, tune's `enough_answers`, test's two model-down messages,
  an allow-class example that clashed with a rule's id, pause, status,
  review); POLICY (unlisted browsers, search results, allow classes and
  whole domains); MODELS (the second look at a silent listener, a mirror's
  wrong file, `model.why`, the log item, nothing hosted while rules.toml
  is broken); README; and this file, which named
  `experiments/serve_capped.py` twice where it no longer exists. Three
  small hunks outside the docs, so that they hold: the `rules test` and
  `tune` hints and decide.py's no-key error run qualm as this terminal
  does, and `status --json` joins its errors without a doubled stop.

Round 3 verified: 411 tests, from the checkout and from a temp folder; each
fix's repro run again before and after, on fakes and in throwaway homes, by
its branch; the guide's commands in a throwaway home. No verification round
was run after round 3. Not verified live, after all three rounds: the app
with the merged code on this Mac; Take me back in real browsers (only
against fake AppleScript, keys and Accessibility), in a real unlisted
browser, and Atlas's bundle id; Arc's and Opera's dictionaries; Firefox
exposing AXURL; Vivaldi's and Chrome's real trees with hundreds of tabs;
Qualm.app from Finder; the login item; the managed server's real first
download, a mirror serving another file, and a busy model behind a real
tunnel; the Facebook, Twitch and Kick patterns on real pages.

**Still open** after all three rounds, in one list (each branch's commit
has the details):

- rules.toml and data: `config undo` covers rules.toml only (`never
  remove`, `except remove` and `pause --stop` undo the rest) and has no
  redo but copying `backups/rules.toml.replaced` back; on a home an older
  Qualm set up whose rules.toml was already broken before any newer
  command ran, the first undo also drops the last saved change (`note`
  says so and how to keep it); a rules.toml moved away is made again from
  the starters (by design: `config undo` brings the last save back);
  usage_history.json is still read strictly.
- Capture: unread counts in the middle of a title ("Inbox (3) -
  me@gmail.com") still change it (a general " (N)" strip would cut "Rocky
  (1976)"); Chrome's memory label stays in Spanish, Brazilian Portuguese and
  Russian (the title sits inside the sentence); Safari's AutoFill lines are
  stripped in English and Chinese only; OCR loses a few short Korean words
  in a mostly English line ("설정 Settings 열기 then 저장 Save", even on a
  Korean Mac: Vision's language detection, and turning it off empties
  Chinese, Japanese and Russian lines; lines with more Korean read right).
- Models and starters: a new rule's 0.2 is a guess, and nothing scores a
  rule when it's added; so a new deny rule (`target = "page"`) can still
  pop up on search results that score just over it (a news rule, 0.205 on
  a Google search);
  Kev and Jev split on thin feed-or-item calls (a margin on P(feed) would
  change every content rule, unmeasured); a live coding stream on Twitch
  pops up (the pattern, and learn vs entertain has no margin), and Not
  this one lets only that exact address through; short video and feeds
  still name Chinese sites first (the variants tried moved nothing or
  lifted LinkedIn's feed to 0.77); `huya.com/\w+` also matches category
  pages; Twitch's /following, /store, /messages, /friends, /bits, /privacy
  and /creatorcamp aren't excluded (as on main), and a Kick name nobody
  registered (/help, /faq: "Channel Not Found") counts as a channel; round
  1's local check of the starter wording covered 40 pages.
- CLI: `rules test` and `review` can't hide titles and URLs from the
  agent's provider (no redaction flag); `rules test` shows no progress
  under `--json` and defaults to `--last 100`; `when` takes no dates
  (`pause --from/--until` covers one-offs); `eval --suggest` still walks
  the threshold down; `policy.OWN_URLS` names only :8765 (a moved
  dashboard is still skipped by its title); doctor says "Your data … stays
  on this Mac" next to the hosted model (true of the logs); change
  commands don't warn when the running app is an older copy (status has
  `app_older`, and the guide says what to do).
- Local model: no "Cancel download" menu item (Hosted or Quit stops a
  download); the setup window doesn't confirm "On this Mac" when memory is
  short; HF_ENDPOINT from the environment isn't checked (the pinned hash
  covers it); the menu's mirror tooltips name a plain `qualm settings set
  hf_endpoint=…`; an unmarked Kev-4B copy saved by main's code isn't
  pruned (4.5 GB; by its folder name it can't be told from a developer's
  `qualm serve --model` build, and this Mac has only the pinned copy); a
  silent listener on 8009 that isn't Kev holds up the app's start by up
  to ~12 s (its second look).
- App: no rule editor by design (rules change through an agent; "Edit
  rules file…" stays); the SDK still retries a refused key; the menu
  doesn't say when the dashboard couldn't start; an
  older copy on a custom home whose environment `ps` can't show is taken
  for the default home's; a custom QUALM_HOME's app still logs to
  ~/Library/Logs/Qualm, so its Model menu offers the main home's model log
  when there is one; `Decision.own` is unused (app.py works it out itself);
  onboard.py imports `NSStackViewGravityLeading` unused.
- Dashboard: each changed refresh still reads judgements.jsonl from the
  start (and trials.jsonl whole for tuning); the pop-up's "2nd time today"
  (`policy.popups_today`) counts a hit re-judged under an open pop-up (the
  dashboard and `qualm week` don't); the dashboard and `rules tune` can
  still disagree on a rewording whose only trace is an unanswered
  judgement older than 14 days, with no kept rules.toml version showing
  it.
- Tests: test_localmodel's moved-port test can fail if the OS hands its
  fake server port 65535 (about 1 in 16,000 runs).
- Everywhere: the interface is English only, and rule ids are ASCII (a
  rule in other letters goes by its description's words); Qualm.app is
  arm64 only (no universal build for Intel Macs) and ad-hoc signed.

### Releases and updates (night of 2026-09-24)

Asked for a beta the tech audience can download: a workflow that builds and uploads releases, an
update check done the way Mac apps do it, and one test release published.

- **Sparkle 2.10.0 in Qualm.app** (`updates.py`, `build_app.py`): pinned by version and SHA-256,
  signed ad-hoc from the inside out (a `--deep` signature would have given its helpers Qualm's
  identifier). Info.plist: the feed (`appcast.xml` on the latest GitHub release), the public EdDSA
  key, a daily check, `SURequireSignedFeed`, `SUVerifyUpdateBeforeExtraction`, and
  `SUAllowsAutomaticUpdates` off: an ad-hoc signed update is a new app to macOS, so Accessibility
  must be given again, and a silent install would quietly stop Qualm. Sparkle's "gentle reminders":
  a daily check never opens Sparkle's window (not even at launch, which it calls immediate focus:
  for a login item, the moment you log in); a menu line and one corner notice per version say so,
  and *Install…* opens Sparkle's window with the notes. After an update, a notice says how to give
  Accessibility again. Menu: *Check for Updates…*, *Check for updates automatically*.
- **A checkout** reads the same feed daily (`data/updates.json`), says when a newer version is out
  and links to it; `qualm version [--check] [--json]` for terminals and agents.
- **Releases** (`packaging/release.py`, `.github/workflows/release.yml`, packaging/README.md,
  "Releases"): a pushed tag `v<version>` runs the tests on an Apple silicon runner, builds, signs
  the DMG and the feed, and publishes the GitHub release with `Qualm.dmg` and `appcast.xml`; the
  notes are the version's section of docs/CHANGELOG.md. The build number is the commit count. Every
  release is marked latest, betas too (a pre-release never reaches the feed). Version 0.1.0b1.
- **The key**: made with Sparkle's `generate_keys` on this Mac, in the login keychain (*Private key
  for signing Sparkle updates*) and the repository secret `SPARKLE_ED_PRIVATE_KEY`. Lose it and no
  copy out there takes another update.
- Also: Qualm saves today's minutes and stops its model server on any quit (Sparkle's, logging
  out), not only the menu's; the Info.plist copyright said Apache-2.0 (the app is GPL-3.0-or-later).

Verified: 425 tests. End to end with the real app bundle, in a throwaway home, paused: a copy
made build 50 read a local signed feed for build 51, downloaded the DMG, checked its signature
before unpacking and replaced itself, and the result passes `codesign --verify --deep --strict`
(the silent-install path, turned on in that copy only, to test without clicking); with installs
left to you, the same check logged the update, showed the menu line and the notice, and changed
nothing. The first try at that showed Sparkle's own window at launch (see above: fixed). The four
notices rendered, light and dark. Not verified: clicking *Install and Relaunch* in Sparkle's window
(Sparkle's own code path; the installer it runs is the one tested), and an update fetched from
GitHub itself (the repository is private: below).

**Qualm 0.1.0b1 is released** (build 52, 2026-09-24 late evening, tag `v0.1.0b1` on 9957e54):
https://github.com/RoderickQiu/qualm/releases/tag/v0.1.0b1, marked latest, with `Qualm.dmg` and
the signed `appcast.xml`. Only the tag was pushed, not `main`: Vercel deploys the website to
production on every push to `main`, and that push needs its own yes. GitHub didn't run the
workflow for the tag (no run at all, twice, the second time with a token that has the `workflow`
scope): a workflow file that exists only on a tag isn't picked up. So the release was built and
published on this Mac with the same script (`release.py --publish`). The workflow takes over once
`main` with it is pushed; then Actions > Release > Run workflow builds and signs without
publishing, to try it. Verified on the published files: they equal dist/ (GitHub's SHA-256 of each
asset), the DMG's and the feed's EdDSA signatures check out against the key, and a copy made build
50 updated itself to this build 52 through Sparkle (served from this Mac). The test copies, their
Sparkle settings (`com.qualm.app` defaults, which didn't exist before) and LaunchServices records
were removed afterwards.

**The repository stays private** (the user's decision, 2026-09-24). While it is, the release, the
feed and the DMG need a GitHub sign-in, so no copy of the app can check for or download updates:
Sparkle's daily check fails quietly, *Check for Updates…* says it couldn't, and `qualm version
--check` exits 5 (unreachable). The website's and README's download links lead to a 404 for
everyone else. Opening the beta means one of: making the repository public (the feed works as
built), or a public place for releases only (a releases-only repository, or the feed on
qualm.r-q.name), which needs a new build: `SUFeedURL` is inside the app (`updates.FEED`).

### About, the skill and the logs (late on 2026-09-24)

Asked: there was no About window, so nothing in the app said who made it, and the menu had no link
to the website; and could another person's Claude Code find Qualm's logs and its skill? It
couldn't: the skill was only in this repo (`.claude/skills/qualm`, a project skill), and the guide
never said where the logs were.

- **About Qualm** (`about.py`): a small window in the setup window's style (system colors, light
  and dark): the icon, the version (`Version 0.1.0 beta 1 (52)` from Qualm.app, `…, from source`
  from a checkout), the tagline, *Made by Tianrun Qiu* linking to r-q.name, Kev and Jev, the GPL,
  and *Visit qualm.r-q.name*. Esc or Cmd-W closes it. The menu's last group is now *About Qualm*,
  *Check for Updates…*, *Quit Qualm*: a separate *Qualm website* item was merged into About (the
  user: fewer entries is better). Info.plist's copyright names the author
  (Finder's Get Info; it takes a rebuild). README ends with "Made by Tianrun Qiu".
- **The skill for Claude Code** (`agent.py`): `~/.claude/skills/qualm/SKILL.md`
  (`CLAUDE_CONFIG_DIR` moves it), the guide with the repo skill's description and a line saying
  how to run `qualm` here, by full path (Claude Code's shell may not have ~/.local/bin). The
  description now also triggers on "Qualm isn't working". Added by the first setup when Claude
  Code has run here (a ticked box on the setup window's last page; `qualm setup` in a terminal the
  first time), by *Qualm skill for Claude Code* in the menu (shown where Claude Code has run), and
  by `qualm skill install`; `qualm skill remove` and `uninstall --all` take it away. The app
  rewrites a skill of Qualm's when it starts on another version, and never adds one: a skill you
  removed stays removed, and a re-run of setup doesn't bring it back. A SKILL.md there without
  the "Written by Qualm" line is someone else's and is never touched. Not for a QUALM_HOME, like
  the `qualm` command. The repo's SKILL.md is the same description and guide (a test holds them
  together).
- **Logs**: `qualm logs [--model] [--lines N] [--json]` lists `~/Library/Logs/Qualm` and prints
  the end of the app's log written last (`com.qualm.app.log` from Qualm.app, or where a checkout's
  output went, `qualm app >> app.log`) or `kev.log`; `status --json` has `paths` (rules, data, logs);
  the guide has "When Qualm itself misbehaves" (doctor first, then logs, and a line per screen in
  the log reaches the agent's provider). Qualm.app opened from Finder or `open` had its output
  sent to /dev/null by macOS, so only a login-item start ever wrote a log: now it goes to
  `com.qualm.app.log` too, a line at a time (`watcher.log_to_file`), and rolls over as the login
  item's does.

- **Open at login** (asked right after: "you don't even have a start at login"): the menu showed it
  from Qualm.app only, since a checkout's login item runs outside the terminal whose Accessibility
  it has. Now it's there from a checkout too; ticking it there says in a notice that macOS asks for
  Accessibility for the Python it starts as (the menu's first line then fixes it in one click). A
  checkout's login item runs `.venv/bin/python3 -m qualm app` directly, not `uv run`: launchd's
  process is the one macOS asks permissions for, so through uv they'd have been uv's, not the
  Python's that `qualm install` names (uv's folder stays on its PATH, for the model's runtime).
  Qualm.app's plist is unchanged, so installed login items stay current. **Fixed**: unticking *Open
  at login* in a Qualm the login item started ran `launchctl bootout` on its own job, which killed
  that Qualm before the plist was removed, so it quit and still opened at the next login. A copy
  started at login (`XPC_SERVICE_NAME` is the job's label) now only writes or removes the plist
  (`autostart.started_at_login`). A refusal (a disk-image copy, a checkout's rules not copied home)
  shows in a notice in full, not cut in the status line, and `install`'s `sys.exit` no longer
  escapes a menu action. Verified under real launchd with test labels, in the background: the
  checkout's command started, found the running app and exited 0 once (no restart loop);
  `XPC_SERVICE_NAME` is the label; a job that unticks itself was killed without the guard (plist
  left) and ran on with it (plist gone). The notice was rendered off-screen.

Verified: the tests (the skill written, kept current, removed, someone else's left alone,
QUALM_HOME refused; setup the first time only; uninstall; `qualm logs` on a pretend log folder;
the menu's About (and its website button) and skill switch, off-screen; a child process with /dev/null for output
writing its log). `conftest.py` points `CLAUDE_CONFIG_DIR` at a temporary folder for every test,
since the setup and uninstall fixtures pretend to be the main install. `qualm skill install|remove`
and `qualm logs` run for real from the checkout (skill into a temporary Claude folder). The About
window and the setup window's last page (three boxes, Qualm.app) rendered off-screen, light and
dark. Claude Code reloaded the repo's skill with the new description mid-session, so the
frontmatter parses. Not verified: the About window on screen, and Qualm.app opened from Finder
writing its log (no app was opened: the user was at the Mac). Your ~/.claude has no Qualm skill
yet: the running app is from before this, so tick it in the menu after a restart, or run
`uv run qualm skill install` (inside this repo the project skill is there already).

### MVP (built after the trials)

`qualm app` is a menu bar app (Python + PyObjC) that watches the
front window, and a floating panel that steps in with three answers: **Take me
back**, **I need it: 10 min** (asks why), and **Not this one** (teaches the rule an
exception). The policy follows docs/POLICY.md: block the mechanism (short
video, feeds, live rooms, compulsive checking), not the site; exempt learning,
work tools, search, sensitive pages and anything opened on purpose. (Search
was only used for "opened on purpose" until round 3 of the audit made
results pages an exemption of their own.)

- Verified: the policy (18 unit tests, no model); `watch` live on this Mac
  (an O'Reilly chapter reads as learn 0.97, nothing flagged); the panel
  renders (`app --demo`); a replayed browsing sequence behaves as designed
  (`experiments/scenario.py`, see Measured).
- Not verified: the app over a real day; "Take me back" (Cmd-[ via System
  Events) in each browser; whether the menu bar item shows on this Mac. The
  "Qualm" item wasn't visible in a screenshot with a full menu bar and Thaw
  running.

Verified working on this Mac (M5 Pro, 24 GB, macOS 27): `probe`, `ask`, `watch`,
`eval`, `export`, `app --demo`. Not exercised: `label` and `harvest` (they need you).

Not built: a Developer ID signature (Qualm.app is ad-hoc signed, so each
rebuild asks for Accessibility again).

## Run it

```bash
uv run qualm setup              # first time: model, permission, rules, login; copies a checkout's rules and data home
                                #   scripted: --backend kev|jev --rules a,b [--login] [--key -] --json; --dry-run first
uv run qualm app                # menu bar + pop-up; starts the local model server itself (or the setup window, first run)
uv run python packaging/build_app.py   # dist/Qualm.app + dist/Qualm.dmg (packaging/README.md)

uv run qualm doctor             # everything checked, with fixes
uv run qualm status             # app running? which model, does it answer? the dashboard's address
uv run qualm serve              # the model server in this terminal: Kev-4B, 8-bit, :8009; --bits 16, --kev-dir DIR (never a second one)
uv run qualm rules list         # your rules; docs/PERSONALIZE.md for the rest
uv run qualm settings set backend=jev   # switch to hosted Jev (needs a key: `qualm setup --backend jev`)
uv run qualm probe              # state only, no model; Ctrl-C to stop
uv run qualm ask --delay 3      # switch windows within 3 s, get one reading
uv run qualm label              # capture + label one moment -> data/labels.jsonl
uv run qualm eval               # precision/recall per threshold, latency
uv run qualm install            # start the app at every login (uninstall to undo)
uv run qualm app --demo         # show the panel once, learn nothing (deny|feed|checkin|timesup|focus|prompt)
uv run qualm focus write the report --minutes 50   # --stop ends it
uv run qualm pause 30           # also 2h, 2d, --until 'mon 09:00', --from 'sat 00:00' --until 'mon 09:00'; --stop resumes and cancels a planned one
uv run qualm watch              # the same loop in the terminal, every judgement printed
open http://127.0.0.1:8765/     # dashboard (the app serves it; or `review --web`; `status` says if it moved)
uv run qualm review             # the same judgements in the terminal; --json, --since, --id, --misses
uv run qualm week               # this week's numbers, as the dashboard's Insights
uv run qualm eval --reviews --suggest      # re-ask the model on everything you reviewed
uv run qualm export --lang en   # labels -> Kev training JSONL
uv run qualm version --check    # this copy's version; is a newer one out? (the feed on GitHub)
uv run qualm logs               # where the logs are, and the app's last lines; --model: kev.log
uv run qualm skill install      # Claude Code's skill for Qualm in ~/.claude/skills/qualm (remove: take it away)
uv run python packaging/release.py   # dist/Qualm.dmg + signed dist/appcast.xml; --publish for a pushed tag (packaging/README, "Releases")
QUALM_HOME=$(mktemp -d) uv run pytest -q -p no:cacheprovider   # the tests, from the checkout; no model, no screen
                                # from another folder: uv run --project <repo> pytest -q <repo>/tests
```

Everything reads and writes `~/Library/Application Support/Qualm` (rules.toml,
data/, kev-env/, models/); logs in `~/Library/Logs/Qualm` (`com.qualm.app.log` from Qualm.app,
`kev.log` from the model server; `qualm logs`). The setup window
shows until setup has left `.setup-done` there (or `data/judgements.jsonl`
exists, a model was chosen, or a `rules.toml.bak` with no `backups/` shows
an earlier version saved the rules). Only one app runs per Qualm folder
(`app.lock`); a second copy says so and exits, and so does a new copy
started while an older one runs that takes no lock ("An older Qualm is
running (pid N): quit it from its menu bar icon first", exit 0). To update:
quit the running app from its menu, update the checkout, then start
`uv run qualm app` (rebuild Qualm.app before opening it: packaging/README).
The app serves the dashboard.html it started with: after an update an open
tab reloads itself (or says "Qualm was updated: reload this page.") and
saves nothing until it has; when you edit dashboard.html in a checkout,
restart the app or `review --web` to see it.

Env knobs: `QUALM_HOME` (a folder other than the default leaves the main
install's login item, shim, key and logs alone in setup and uninstall;
`setup --login` there refuses to replace a login item that starts another
home, and `qualm install` run with that QUALM_HOME replaces it and says so;
an app from before `/api/hello` counts as running only for the default home),
`QUALM_BACKEND=kev|jev` (over `[settings] backend`),
`QUALM_MODEL`, `QUALM_SHIFT` (log-odds; default 0 for Kev, 2 for Jev),
`KEV_URL` (the model server; without it, the port in `models/server.json`
while the app's moved server runs, else :8009), `KEV_TIMEOUT` (default 30 s;
the SDK's 10 s is shorter than 4B's warm-up),
`QUALM_QUESTION_STYLE=rule|direct` (see Measured),
`QUALM_DASHBOARD_PORT` (over `[settings] dashboard_port`; 8765 by default,
and a free port with data/dashboard.json when that's taken), `HF_ENDPOINT`
(over `[settings] hf_endpoint`, which must be https), `QUALM_BUILD=1` (build
the 8-bit copy here if the published one is gone),
`QUALM_PREBUILT=repo@revision` (checked against its own provenance.json,
not the pinned hash), `QUALM_KEV_ARCHIVE` (a copy of the Kev archive on
GitHub, where GitHub is blocked; `qualm serve` once installs the runtime
from it). The app's model server exits with the app (`QUALM_PARENT`).

Permissions: **Accessibility** for Qualm.app, or for the terminal when run
from a checkout. **Screen Recording** (optional; *Screen & System Audio
Recording* on macOS 15 and later) for OCR of windows drawn as pixels and
the dashboard's screenshots. **Automation**, asked the first time Take me
back runs there: for each Chromium browser (its AppleScript), for Safari
(its AppleScript reads the address) and System Events (Safari's Back), and
for System Events alone in Firefox and the other browsers driven by keys.

## Layout

| File | What it does |
|---|---|
| `src/qualm/state.py` | Front window → `ScreenState` → compact `state` dict, cut to a character budget; the browser table (`CHROMIUM`, `BY_KEYS`, `BROWSERS`), `page_address`, and `browser_window` (a browser missing from the table, known by its window) |
| `src/qualm/rules.py` | Rules from `rules.toml` (checked on load: fields, types, sites, `when`, regexes); builds the typed questions |
| `src/qualm/config.py` | Edits rules.toml one key at a time, comments kept; each change under a lock (`rules.toml.lock`), saved whole, the last 20 versions in `backups/` for `config undo`, and the file as Qualm last wrote it (`backups/rules.toml.saved`; an older Qualm's file as first seen), so undo after a hand edit goes back to it first; what an undo replaces or passes over is kept (`.replaced`, `.aside`); `undo_to`, undo's dry run, for the advice every load error gives; refuses a file that wouldn't load |
| `src/qualm/jsonl.py` | Reads the data logs, skipping a line cut short (with one note on stderr), and appends to them (`jsonl.appending`: a torn last line gets a newline before the next record) |
| `src/qualm/agent.py` | The menu's prompt for an AI agent, the `qualm` command a stock shell can run, and Claude Code's skill for Qualm (`~/.claude/skills/qualm`: written, kept current, removed) |
| `src/qualm/about.py` | About Qualm: the version, the maker, the website |
| `src/qualm/personalize.py` | The `rules` / `allow` / `except` / `never` / `settings` commands |
| `src/qualm/trial.py` | `rules test` / `label` / `tune`: a rule on your recent screens, and its threshold from your answers |
| `src/qualm/decide.py` | TypeSafe SDK client (Kev or Jev, from `[settings] backend`), the score shift, `ask()` |
| `src/qualm/paths.py` | Where things live: ~/Library/Application Support/Qualm, a checkout's legacy folder, the bundle, uv |
| `src/qualm/keychain.py` | The TypeSafe key in the login keychain (and .env / the environment first) |
| `src/qualm/localmodel.py` | The local model: runtime install, server command, the app's managed server (and `models/server.json` when it moved off 8009), where every model call finds the server (`server_url`), memory in numbers |
| `src/qualm/kevserve.py` | Kev's server as Qualm runs it (inside the runtime): MLX cache cap, 8-bit, weights saved once |
| `src/qualm/setup.py` | First run, shared by `qualm setup` and the window: migrate, recommend, apply |
| `src/qualm/onboard.py` | The setup window: a 5-page assistant (Auto Layout) |
| `packaging/` | `build_app.py` + `launcher.c`: Qualm.app (Sparkle inside) and the DMG; `release.py`: the signed DMG, the signed feed, the GitHub release; README on signing and releases |
| `src/qualm/updates.py` | Is there a newer version: the feed (appcast.xml on the latest release), versions compared, the checkout's daily check, Sparkle started in Qualm.app |
| `.github/workflows/release.yml` | A pushed tag `v<version>`: tests, build, sign, publish |
| `docs/CHANGELOG.md` | Each version's release notes |
| `src/qualm/policy.py` | Reading -> skip / allow / intervene: thresholds, URL patterns, exemptions, "opened on purpose", check-in sessions and their waits, snoozes, re-judging, user exceptions, the decision log |
| `src/qualm/watcher.py` | The loop shared by `watch` and `app`; "take me back" |
| `src/qualm/app.py` | Menu bar item (focus, pause, the problems line and icon, the key window), the intervention panel, the corner notice and the focus prompt (PyObjC); the one-copy lock and the older-copy check; the rules it starts on when rules.toml doesn't load (`fallback_config`); serves the dashboard |
| `src/qualm/ui.py` | The look: the blurred HUD panel (`QualmPanel`, which ignores keys for 0.6 s), labels, badges, the screen dim |
| `src/qualm/explain.py` | The pop-up's words: headline, reason, the neutral context line, which part of the screen carried it; rule names |
| `src/qualm/review.py` | Dashboard server (http://127.0.0.1:8765 or the port in data/dashboard.json): paged data, insights, verdicts, thresholds, exceptions, focus/pause, rule on/off, `/api/hello` (which app, its permissions) |
| `src/qualm/dashboard.html` | The dashboard page: Today, Review, All screens, Insights, Rules |
| `src/qualm/doctor.py` | `qualm doctor` |
| `src/qualm/autostart.py` | `serve`, `install`, `uninstall`: the model server in a terminal, the app's LaunchAgent, the `qualm` shim |
| `src/qualm/cli.py` | `probe` / `ask` / `app` / `watch` / `label` / `harvest` / `eval` / `export`, plus the commands above |
| `src/qualm/rules.example.toml` | The starter rules and allow classes, in English, with measured thresholds and why in `note` |
| `docs/MANUAL.md` | The long README: every feature, setup in full (permissions, mirrors, from source), commands, the numbers, privacy |
| `docs/PERSONALIZE.md` | Every personalization command, the question limit, and the test-then-tune loop for a new rule |
| `.claude/skills/qualm/SKILL.md` | How Claude Code should drive the CLI for a user: the guide with the skill's description (agent.py installs the same for Qualm.app users) |
| `docs/MODELS.md` | On this Mac vs hosted: speed, memory, disk, Jev's usage and cost, from measurements |
| `docs/POLICY.md` | What to block on a desktop and what not, and how it generalizes and personalizes |
| `tests/` | 445 tests, no model and no screen: the policy with made-up readings (`test_policy.py`), the menu bar and setup window built off-screen (`test_app.py`), the CLI's JSON contract and the guide's commands (`test_cli.py`), and the rest by module |
| `experiments/` | `demo_data.py` (made-up weeks for screenshots, with the exceptions their answers make); trial tooling: `collect.py` + `manifest.py` (scripted pages, captured from a background Safari window via `bg.py`), `analyze.py` (per-rule threshold sweep and AUC over `eval --dump`), `state_tokens.py`, `relabel.py`, `simulate.py` and `scenario.py` (see Measured) |

`rules.toml` and `data/` are git-ignored: they contain what you read on screen.
In `data/`, the app keeps `judgements.jsonl` (every judgement: the capture,
exactly what the model read, every answer's probabilities, the screen before,
what the policy did and why; sensitive pages and unmonitored apps without
content; since the audit also each rule's wording key `w`, the `backend`
that scored it, and in `decisions` the pop-up's decision id), `shots/` (a
~900 px screenshot per new screen, only with a reading that found the page
not private), `reviews.jsonl` (your answers on the review page),
`decisions.jsonl` (interventions, your answers, focus sessions), `session.json`
(pause and focus now, and `pause_later` for a planned pause; `.session.lock`
beside it), `reflections.jsonl` (the weekly question), `exceptions.jsonl` ("Not
this one", "Never here", `never` and `except add --from`; typed exceptions
now go to rules.toml, and older ones typed on the dashboard stay here),
`trials.jsonl` (`rules test` scores, per exact rule wording and model, with
the raw score), `status.json` (the running watcher's pid and whether the
model answers, for the dashboard's chip; the app's own dashboard always
shows itself running and asks for Accessibility live, so only `review
--web` goes by it, and a `qualm watch` that quits no longer makes the app
look stopped), `dashboard.json` (the port, when 8765 was taken) and
`usage.json` (today's minutes and check-in sessions per rule, read
leniently: a torn file is skipped with a note naming it;
`usage_history.json` keeps every day). Next to rules.toml: `backups/` (its
last 20 versions; `rules.toml.saved`, the file as Qualm last wrote it;
`rules.toml.replaced`, what the last undo replaced; `rules.toml.N.aside`,
kept versions an undo passed over),
`rules.toml.bak` (the newest version), `rules.toml.lock`, `app.lock`,
`kev-env.lock` (one runtime install at a time) and `.setup-done`; in
`models/`, `server.json` (the address the app's model server moved to,
while it runs).

Never flagged: `[[allow]]` classes in rules.toml are kinds of page described
in words (shipped: shopping, "an online store: a product page, listing, cart
or checkout"); if the model says a page is one, no rule fires there except a
URL pattern. On the trial pages plus real store pages, stores scored
0.42-0.92 and every other page 0.18 or less, hence threshold 0.35. Music
ships as a second class (a player scored 0.96-0.97; every trial page,
YouTube and Douyin included, 0.16 or less), with known music sites and apps
(YouTube Music, Spotify, NetEase, QQ Music…) listed under `patterns` / `apps`
so they're allowed without a model call.

The stocks threshold went from 0.13 to 0.3 after real use: WhatsApp chats
about shopping scored up to 0.27, while trial stock pages mostly score
0.27-0.69. Stock forums (Guba, r/wallstreetbets, Stocktwits, Xueqiu), which
the model scores low, are URL patterns instead. Whole policy on the trial
set: precision 0.96, recall 1.00 (was 0.96 / 0.92). The pop-up's
"Never in <app>" / "Never on <site>" does the same for one app or site.

There is no testing mode any more: deny rules step in at once, check-in
rules ask on arrival (above, and docs/POLICY.md).

## Design decisions, and why

1. **Text, not pixels.** Jev has no image input yet. On a desktop, the window
   title, URL and page headings carry most of the signal for a browser, which
   is where most distraction happens.
2. **Small state: `--budget` 700 characters by default.** Kev was trained on at
   most 384 state tokens, and its README says longer context wasn't covered.
   The state keeps app, title and URL, then headings, then body text, until the
   budget runs out. `label` stores the *full* capture, so `eval --budget N`
   can re-cut it to test different sizes.
3. **The prose prompt becomes typed questions.** The Android prompt
   (SeeNot's `app/src/main/java/com/seenot/app/ai/screen/ScreenAnalyzer.kt`,
   around lines 1408–1516) does everything in one prompt. Here it is split:
   - `sensitive`: a yes/no question.
   - `page_kind`: feed / single_item / search / work / other.
   - `rule_<id>`: violates/safe/unknown for DENY rules, in_scope/out_of_scope/unknown
     for check-in rules (TIME_CAP on Android).

   The logic that combines them is plain code in `Gate`. For example, a
   content rule never fires on a feed page; the Android prompt needed a page of
   prose rules to say that.
4. **Act on probabilities, with a threshold per rule.** Kev-4B ranks well but
   its `p_hit` runs low, so each rule has its own `threshold` from `eval
   --suggest` (0.13-0.5 today). The first design's global 0.85 / 0.5 Gate and
   its two-reading nudge were dropped: at 0.85, stocks recall was 0.
5. **Repair rules become `exceptions`** in each rule's question text. That's
   the desktop version of SeeNot's false-positive → repair-rule loop
   (`FalsePositiveRuleGenerator.kt`). The panel's "Not this one" adds them.
6. **Local by default.** A hosted model would receive whatever is on your
   screen every few seconds. Jev is a switch, not the default.

## Measured

### Trial data (read this before trusting any number below)

`data/auto_labels.jsonl`, 119 records, made by `experiments/collect.py`:
pages loaded one by one in a background Safari window (a blocker extension
in Chrome hid YouTube Shorts) and captured through the same Accessibility
walk as `capture()`, plus one Cursor window. Labels come from what the page
is, decided by the script author, not by you:

- shortvideo violations 27 (YouTube Shorts, Bilibili funny clips, Bilibili
  live rooms, Twitch channels, Douyin, Kuaishou, TikTok)
- stocks violations 25 (Yahoo, Google Finance, Xueqiu, Eastmoney, Guba,
  Stocktwits, TradingView, Sina, CNBC, MarketWatch, Investing, r/wallstreetbets)
- social in_scope 20 (X, Weibo, Xiaohongshu, Jike, Reddit, Threads, Bluesky,
  Instagram, Facebook). Logged out, so many are login or sign-up walls.
- 15 sensitive (login pages), plus feeds, search, docs, news and work pages.

It is logged-out, browser-only and easier than real use: most positives have
the answer in the URL or title. Treat it as a first filter, not the verdict.

### Results, 119 records, `--budget 700`

"Recall at P>=0.9" is the best recall at any threshold where precision is at
least 0.9 (`experiments/analyze.py`). At the Gate's actual 0.85 threshold
recall is much lower; see below.

| Model | Question style | Lang | shortvideo AUC / recall | stocks AUC / recall | social AUC / recall | page_kind acc | sensitive acc | latency p50 / p95 |
|---|---|---|---|---|---|---|---|---|
| 0.8B | rule | zh | 0.91 / 0.33 | 0.94 / 0.72 | 0.61 / none | 0.63 | 0.88 | 161 / 300 ms |
| 0.8B | rule | en | 0.92 / 0.37 | 0.97 / 0.12 | 0.53 / 0.10 | 0.63 | 0.88 | 160 / 268 ms |
| 0.8B | direct | zh | 0.95 / 0.78 | 1.00 / 1.00 | 0.82 / 0.35 | 0.63 | 0.88 | swap-affected |
| 0.8B | direct | en | 0.95 / 0.78 | 1.00 / 1.00 | 0.84 / 0.35 | 0.63 | 0.88 | swap-affected |
| **4B** | **rule** | zh | 0.99 / 0.74 | 1.00 / 1.00 | 0.98 / 0.50 | 0.82 | 0.98 | 880 / 1144 ms |
| **4B** | **rule** | **en** | 0.98 / 0.85 | 1.00 / 1.00 | 0.99 / 0.90 | 0.82 | 0.98 | 847 / 1032 ms |
| 4B | direct | zh | 0.96 / 0.48 | 1.00 / 1.00 | 0.93 / 0.55 | 0.82 | 0.98 | 846 / 1012 ms |
| 4B | direct | en | 0.94 / 0.74 | 1.00 / 1.00 | 0.97 / 0.60 | 0.82 | 0.98 | 855 / 1059 ms |

Best setup: **Kev-4B, rule style, English rule text.** Thresholds that reach
precision >= 0.9 there: shortvideo 0.15, stocks 0.06, social 0.20.

"direct" (`QUALM_QUESTION_STYLE=direct`) asks "Is the open content X?"
instead of "the user forbids X: violates/safe?". It rescues 0.8B (stocks 0.12
-> 1.00 recall) but makes 4B slightly worse, so the default stays "rule".

### What goes wrong

- **Calibration, not ranking.** With 4B, rule style, English, at the Gate's
  thresholds: recall at `p_hit >= 0.85` is shortvideo 0.37, stocks **0.00**,
  social 0.15. Precision is 1.00 at every threshold from 0.3 up. On a Yahoo
  AAPL quote page the answer is `safe 0.55 / violates 0.27 / unknown 0.17`:
  right order across pages, wrong absolute level.
- **Livestreams are missed.** All four Twitch channels score p_hit 0.04-0.07:
  the captured text is a channel name and chat, and nothing says "live".
- **Social is weaker in Chinese.** Reddit threads and logged-out walls sit at
  0.17-0.39.
- **The feed rule in Gate** drops r/wallstreetbets and the Guba list pages,
  which are stock discussion in their entirety.

### MVP policy (six rules, `rules.example.toml`, Kev-4B, English)

Relabelled trial set `data/auto_labels_v2.jsonl` (`experiments/relabel.py`).
Same caveats as the trial data, plus: livestream has 8 positives on 2 sites and
videos 6 on 1 site, so their numbers are weak.

Model alone (`eval --suggest`, then `experiments/analyze.py`), where "held-out"
means the threshold was picked with that site's pages removed:

| Rule | AUC | Precision / recall, all sites | Held-out sites |
|---|---|---|---|
| shortvideo | 1.00 | 0.93 / 1.00 | 1.00 / 1.00 |
| feeds | 0.85 | 1.00 / 0.47 | 1.00 / 0.33 |
| livestream | 1.00 | 1.00 / 1.00 | 1.00 / 0.50 |
| videos | 1.00 | 1.00 / 1.00 | - |
| social | 1.00 | 0.90 / 0.95 | 0.90 / 0.90 |
| stocks | 0.99 | 0.92 / 0.92 | 0.88 / 0.92 |

Generic questions: purpose accuracy 0.92, page_kind 0.82, sensitive 0.98.
Latency with 9 questions: p50 1.2 s, p95 1.5 s.

The whole policy (`experiments/simulate.py`: thresholds, patterns, feed_hit,
exemptions, allow_urls; each page judged cold). Without URL patterns is what a
site nobody listed gets:

| Rule | With patterns: P / R | Without patterns: P / R |
|---|---|---|
| shortvideo | 1.00 / 1.00 | 1.00 / 0.77 |
| feeds | 1.00 / 0.60 | 1.00 / 0.60 |
| livestream | 1.00 / 1.00 | 1.00 / 1.00 |
| videos | 1.00 / 0.83 | 1.00 / 0.83 |
| social | 0.88 / 0.70 | 0.88 / 0.70 |
| stocks | 0.96 / 0.92 | 0.96 / 0.92 |

- `feed_hit` (page_kind = feed and purpose = entertain) lifts feeds from 0.40
  to 0.67 recall on the model's readings, with no false positives.
- Most social misses are logged-out login walls, which are sensitive and so
  skipped by design. The genuine misses: X profiles and Guba lists read as
  single items, not feeds.
- The simulation found a bug, since fixed: feeds were skipped for time caps
  too, so scrolling Weibo didn't count toward the social budget.

`experiments/scenario.py` replays a sequence through the watcher with its
history:

| Screen | Result |
|---|---|
| Google search -> Reddit thread from it | thread allowed: opened on purpose |
| -> r/popular | intervene (feeds); social counted |
| Bilibili calculus course | nothing |
| Bilibili comedy clip | counted toward the video budget |
| Bilibili home | intervene (feeds) |
| YouTube Short, then the next | intervene, both |
| Yahoo NVDA quote | stocks visit 1 of 3 |
| Apple 10-K on sec.gov | allowed without a model call |

### Latency against the number of rules (Kev-4B, this Mac, 2026-09-22)

Synthetic rules, real screens, one call with all the rule questions (plus
the 3 shared ones); median of 4 screens (2 for the grouped rows), with the
app sharing the server:

| Rules | Tokens | One call | In calls of 10 rules |
|---|---|---|---|
| 1 | 498 | 0.4 s | |
| 5 | 788 | 0.4 s | |
| 10 | 1159 | 0.6 s | |
| 25 | 2259 | 1.8 s | 4.9 s |
| 50 | 4105 | 7.9 s (4.5-17.7) | 5.7 s |
| 100 | ~8000 | over 30 s: timed out, and swapped the Mac (14 GB swap; the app's calls timed out until it recovered) | 10.9 s |

Kev answers every question in one forward pass and generates nothing, so up
to ~10 rules costs about the same as one. Past that, one long pass grows
faster than linearly on this Mac (memory, attention over thousands of
tokens); calls of 10 rules keep it linear at ~1 s per 10. Hosted Jev on a
GPU is unmeasured. Hence `[settings] max_questions = 25`, enforced. Not
built: grouping rules into calls automatically, or a first cheap question
that picks which rules to ask; either would let the limit rise.

### Real use, first day (2026-09-22, 1,242 judgements)

- Latency, bf16 Kev-4B, 5 rules + 2 allow classes: p50 1.8 s, p95 8.5 s,
  and p50 11.5 s in the slowest hour. The trials measured 0.85 s. The
  difference was swap: the server's resident set down to 57 MB (its
  weights paged out).
- 20 pop-ups. True: a feed and a forum thread (went back). False, since
  fixed: chat windows showing only a name (now "too little on screen"), a
  Chrome address-bar dropdown, Qualm's own review page, music.youtube.com
  (now an allow class), an ads console page (feed_hit, now needs
  P(entertain) >= 0.6).
- 104 judgements skipped as sensitive: 60 Chrome pages, 40 the lock screen.
  Not audited: a false "sensitive" hides a page from every rule.

### 8-bit and 4-bit Kev-4B (2026-09-23, 119 trial pages, current rules)

`KEV_QUANT_BITS` in `serve_capped.py` (now `src/qualm/kevserve.py`)
quantizes after the LoRA merge (`mlx.nn.quantize`, group 64; the pointer
head stays fp32). Dumps in data/runs/{bf16-en-now,q8-en,q4-en}.jsonl.

| | bf16 | 8-bit | 4-bit |
|---|---|---|---|
| memory (phys_footprint, after eval) | 9.9-11 GB | 6.1-7.1 GB | 4.1 GB |
| latency p50 / p95 | 1.0 / 1.2 s | 1.1 / 1.6 s | 1.0 / 1.5 s |
| AUC shortvideo / feeds / livestream / videos / social | 1.00 / 0.85 / 1.00 / 1.00 / 1.00 | same | 1.00 / 0.82 / 1.00 / 1.00 / 0.99 |
| social P / R at 0.3 | 0.90 / 0.95 | 0.90 / 0.95 | 0.94 / 0.85 |
| page kind agrees with bf16 | | 119 / 119 | 112 / 119 |
| max \|dp\| per rule | | 0.012-0.038 | 0.12-0.44 |

8-bit is the default: the same answers in half the memory. It isn't
faster on a Mac with memory free; it's faster on this one because it
doesn't swap. Latency here ran with the live bf16 server resident, so it
understates 8-bit. 4-bit moves social and videos scores enough to lose
real hits; not offered. Warm, alone: 0.58 s for 10 questions (8-bit).

### Hosted Jev vs local Kev (2026-09-23, 119 trial pages, current rules, English)

`QUALM_BACKEND=jev`, model `jev-1.13.0` (`jev-latest` resolves to it but
answered in 3.5 s, not 0.2). Dumps: data/runs/{q8-en,jev-en}.jsonl; only
the scripted trial pages were sent, none of your own screens.

| | Kev-4B 8-bit, local | Jev 1.13.0, hosted |
|---|---|---|
| latency p50 / p95 | 1.1 / 1.6 s | 0.18-0.20 s / 0.8 s (one run had spikes to 6 s) |
| AUC shortvideo / feeds / livestream / videos / social | 1.00 / 0.85 / 1.00 / 1.00 / 1.00 | 1.00 / **0.92** / 1.00 / 1.00 / 1.00 |
| held-out sites P / R: feeds, livestream | 1.00 / 0.33, 1.00 / 0.50 | 1.00 / 0.53, 1.00 / 0.88 |
| page_kind / purpose / sensitive accuracy | 0.82 / 0.92 / 0.98 | 0.83 / 0.95 / **0.80** |
| scores on hits | low (thresholds 0.15-0.5) | confident (a Short: violates 0.97) |

Jev ranks as well or better, but **run with Kev's settings the policy
falls apart**: feeds P/R 0.23 / 0.20, social 0.33 / 0.10. The cause is
`sensitive`: both models separate private pages equally well (AUC 0.994),
but Jev's scores run high. Every private page scores 0.89+, and so do
public ones (x.com/NASA 0.97, Bilibili home 0.85, Instagram 0.87), so the
policy's fixed `sensitive >= 0.5` skips feeds and profiles. It is Kev's
calibration problem in reverse.

**The fix: one shift for everything.** Jev's probabilities are moved down
by 2.0 in log-odds (`decide.SHIFT`, applied in `ask()` to every score;
`raw` keeps the model's own) so they sit on Kev's scale, and rules.toml,
`sensitive >= 0.5`, `ENTERTAIN_MIN` and the allow classes stay as they
are. Measured live through `ask()` (data/runs/jev-shift2-en.jsonl), with
the shipped rules unchanged:

| Policy P / R | Kev-4B 8-bit | Jev, shift 2 |
|---|---|---|
| shortvideo | 1.00 / 1.00 | 1.00 / 1.00 |
| feeds | 1.00 / 0.60 | 1.00 / **0.67** |
| livestream | 1.00 / 1.00 | 1.00 / 1.00 |
| videos | 1.00 / 0.83 | 1.00 / **1.00** |
| social | 0.88 / 0.70 | **1.00** / 0.70 |
| sensitive accuracy | 0.98 | 0.97 |

Without URL patterns only shortvideo changes (recall 0.77, as with Kev).
Any shift from 1.5 to 3 gives nearly the same table (2-2.25 best), so
it's not a knife-edge fit; it's still one parameter fitted on 119 easy
pages. Per-rule thresholds tuned for Jev instead did worse (feeds 0.91 /
0.67, social 0.88 / 0.70). Stores still clear the shopping class (Amazon
0.81, Apple Store 0.48 vs 0.35), every other page 0.03 or less.
Scores logged, tuned and tested (`rules test` / `tune`) are all shifted,
so thresholds carry over between backends (true since the audit: before it,
`rules test` and `tune` used Jev's raw scores). Off the trial set the two
models agree less: on 53 made-up pages, pop-up or not differed on 5
(docs/MODELS.md, "Where the two disagree").

Jev sends each new screen's text to TypeSafe, so it stays a switch, not
the default.

### State size (Kev tokenizer, state only)

| `--budget` | p50 | p95 | max | over 384 |
|---|---|---|---|---|
| 300 | 123 | 223 | 289 | 0/119 |
| 700 (default) | 176 | 279 | 510 | 2/119 |
| 1500 | 176 | 358 | 520 | 3/119 |

Budgets above 700 barely change anything: `HEADING_LIMIT=8` and
`TEXT_LIMIT=12` cap the state first.

### Earlier (first session)

- Screen capture ~0.3 s wall including `uv run` startup.
- Kev-0.8B first call 4.6 s, then ~70 ms for a short state.

## Next steps, in order

0. **Switch to the audited code, in this order.** The audit is merged
   into main (2026-09-24), but an app started before that (`qualm app`
   from the checkout) is still the old code in memory. Until it's restarted,
   actions that load a module for the first time (the dashboard's data,
   some menu items) may fail; the watching loop itself is safe (its one
   late import, ocr.py, is compatible). `dist/Qualm.app` was rebuilt from
   the merged code the same morning (see step 4).
   1. Quit the running app from its menu bar icon (Quit). Don't use the new
      CLI or an agent before that: the old app can't load keys the new
      commands write (`apps`, `overrides_allow`: `rules add` of a shopping
      or games rule writes them), refuses every reload after one ("rules.toml
      not reloaded"), and the command still says it worked. The model
      server on 8009 can stay: the one running on 2026-09-24 was started
      before this app, so Quit leaves it, and the new app uses whatever
      model server answers there.
   2. `uv sync` in the checkout. The three rounds are one commit on main;
      their 25 separate commits (one per area and round, with what each
      fixed and why) are kept on the branch `fresh-user-audit-history`.
   3. Start `uv run qualm app` again. `uv run
      qualm status` should say "app: running", not "an older copy". Its
      start records your rules.toml as the version Qualm last saved (your
      home has no backups/ yet), so a later hand edit can be undone on its
      own; if it doesn't load then, fix it first (`qualm config check`).
   4. Only if you want Qualm.app: dist/Qualm.app was rebuilt from the
      merged code (after any later change, rebuild it with
      `uv run python packaging/build_app.py`). Quit the checkout's app,
      open dist/Qualm.app (or drag it to Applications), and turn on
      "Qualm" in System Settings > Privacy & Security > Accessibility (each
      rebuild needs it again; remove the old entry).
   Then check live what the audit couldn't touch: Take me back in Chrome,
   Safari and Firefox (Safari asks to control Safari and System Events) and
   in a browser Qualm doesn't list (one Back, every tab kept), a Chrome or
   Vivaldi window with hundreds of tabs, the
   menu's warning line (turn Accessibility off and on), a second `qualm app`
   stepping aside, a broken rules.toml (the app keeps watching and says so),
   and on a spare Mac or account, Qualm.app from Finder with Open Anyway and
   the managed server's first download. Your own rules.toml keeps its old
   wording, sites and notes (its short-video note still says a video
   someone sent is fine; on the rule's own sites it now pops up): to take
   the new starter wording, `rules test videos --what "…"` on your screens,
   then `rules set videos what="…"` (the same for social and livestream),
   or leave it.
1. **Use the app for a few days, and review.** Browse normally, then go
   through the dashboard's Review tab: "Was Qualm right?" per card; for
   wrong ones, "Is this X?" per rule. Use the menu's "This should have been
   blocked" for misses as they happen. Try a focus session a day.
2. **Re-tune from your answers** on the Rules tab (suggested thresholds,
   Apply; exceptions in your own words), or `rules tune ID --apply`. When
   the wording of a rule is the problem: `rules set ID what="..."`, then
   `rules test ID` and `rules tune`. Not run yet: the CLI loop on a rule
   someone new writes from scratch, with real answers.
3. **Offer the 8-bit copy to Kev upstream** (published 2026-09-23 as
   RoderickQiu/kev-4b-mlx-8bit, Apache-2.0, credited, marked unofficial):
   Kev might host it officially; and ask whether PyTorch can be optional for
   MLX serving (most of the 1 GB runtime). Kev-4B, its base and the copy are
   pinned (`localmodel.MODEL`, `BASE`, `PREBUILT`), so upstream commits
   change nothing until the pins move; to take a new checkpoint, publish a
   new copy and bump MODEL and PREBUILT together. Qualm builds locally only
   if the copy is gone and the Mac has 16 GB of memory and 14 GB of disk to
   spare (`QUALM_BUILD=1` forces it).
4. **Close what the audit left open** (its "Still open" list, after round
   3), starting with what a fresh user meets first: `rules test` without
   titles and URLs for agents whose provider shouldn't see them; a way to
   score a new rule when it's added, so 0.2 isn't a blind guess; a
   feed-or-item margin so Kev and Jev agree on thin calls; change commands
   that warn when the running app is an older copy.
5. **Fine-tune only on a CUDA box or Modal**, once there are a few hundred of
   your own labels:
   `qualm export --lang en --labels data/labels.jsonl --out train.jsonl`, then
   `uv run python -m kev.train --data train.jsonl --init_from jaredpalmer/kev-4b --base Qwen/Qwen3.5-4B-Base --epochs 2 --lr 2e-5 --batch 1 --accum 8 --dtype bf16 --device cuda`.
   `--base` is required with `--init_from`; the default base is Qwen3-0.6B.
6. **Ship it:** releases are a tag away (packaging/README.md, "Releases"), and Qualm.app updates
   itself through Sparkle; 0.1.0b1 is released on the private repository. To open the beta: decide
   where releases are public (above: the repository, or a releases-only place with a new
   `SUFeedURL`), and push `main` so the workflow runs for the next tag (that push also redeploys
   the website). Left after that: a Developer ID and notarization (then no Open Anyway, and
   Accessibility survives updates; the workflow would sign and notarize), a universal build if
   Intel Macs matter (hosted works there), and a translated interface. The website's Download
   button could point straight at `releases/latest/download/Qualm.dmg`. Licensed
   GPL-3.0-or-later (LICENSE).

## Known problems and gotchas

- **After the audit**, what's still open is listed at the end of "A fresh
  user's audit". The ones a user meets first: a Twitch coding stream pops
  up (the pattern; Not this one lets that one address through, and the
  same channel with `?sr=a` is another; Never here lets all of Twitch
  through); switching between Kev and Jev changes a few decisions off the
  trial set; the interface is English only.
- **An older app and the new CLI don't mix**: an app from before the audit
  refuses a rules.toml with `apps` or `overrides_allow` and keeps its old
  rules until restarted, while the command reports success. Quit it before
  using the new commands (step 0).
- **While the model can't answer** (a first download, a swapped Mac's
  timeouts, a hosted outage), a rule's own sites and apps still step in,
  and nothing is found private: a site that is also a login page pops up
  over the password field. The starters list none; keep it in mind when
  adding sites.
- **`rules test` on the local model holds up the app**: Kev answers one
  request at a time, 0.5-1.5 s each here, so `--last 300` keeps the app's
  own readings waiting for minutes. The guide says to start at 50-100; a
  run cut short resumes from the trials cache.

- **Chromium/Electron** only build the web Accessibility tree after a client
  sets `AXManualAccessibility`. `capture()` does this every time. The set call
  itself returns -25205, but the tree still appears, so a very first capture
  may come back without page text: retried once, 0.3 s later (2026-09-23).
- **Kev's own caveats** (README):
  - 4.0% of answers get ≥ 0.9 confidence while wrong.
  - The order of options can change the answer.
  - The server handles one request at a time.
- **The `feed` rule in `Gate`** means a feed page never triggers a content
  rule. That's right for "don't show me X", but wrong if a rule is meant to
  forbid the feed itself (SeeNot's prompt handles that case; this doesn't yet).
- **`input_tokens` counts questions too.** The state alone is measured above
  (`experiments/state_tokens.py`).
- **Memory.** Kev-4B + a second Kev server + normal apps overflows 24 GB. Run
  one server: the app's own, or `qualm serve` (both refuse to load a second
  one). Swapping shows up as multi-second latency and SDK timeouts, not as
  errors.
- **Fixed 2026-09-23: Safari's AutoFill popover leaked into later captures.** After a sign-in page,
  "Apple Account / Continue with Touch ID / <your name>" stayed in the window's
  AX tree for the next 11 pages, which makes ordinary pages look like login
  pages. Stripped from the trial data; `capture()` now drops it (fork A).
- **Background Safari windows in Stage Manager have no web AX tree.** Only
  matters for `experiments/collect.py`, which needs the Safari window visible
  (not parked in the Stage Manager strip). The watcher reads the front window,
  which is always rendered.
- **Chrome's address-bar dropdown is a web area too.** `capture()` used to
  take it for the page (URL `chrome://omnibox-popup...`), so the model saw a
  title and nothing else; that's how a store page scored 0.15 for stocks.
  Chrome's internal URLs are now skipped.
- **The Kev server of 2026-09-22 was started from the old checkout's
  experiments/serve_capped.py**, gone since the rename (the script is `src/qualm/kevserve.py` now, and the server on
  8009 on 2026-09-24 runs it). Start one with `qualm serve`, or let the app
  run its own.
- **Headless Chrome screenshots of the dashboard don't exit** (the page
  refreshes every 15 s); the file is written anyway. /tmp/shoot.py-style:
  a timeout of 30 s per shot.
- **Harvested labels cover one rule each** (the rule that fired), and only hits
  the panel showed. Misses need `label`.

## Background and sources

- SeeNot Android source: a variant of [seenot-app](https://github.com/RoderickQiu/seenot-app). See `ConstraintEnums.kt`
  (DENY / TIME_CAP / NO_MONITOR) and `ScreenAnalyzer.kt`.
- Offline eval from the SUSTech thesis: WeChat precision went from 79.3% to
  93.0% after 17 repair rules; Taobao from 75.9% to 98.4%. That dataset is
  Android screenshots and does not transfer; desktop needs new labels.
- Jev: https://typesafe.ai/blog/introducing-system-one-models-and-jev
- Jev API (Python SDK `typesafe-sdk`, installed at 0.7.1): https://dev.to/valyuai/how-to-use-jev-a-practical-guide-to-typesafes-system-one-model-g5e
- Kev: https://github.com/jaredpalmer/kev (MLX backend: PR #43)
