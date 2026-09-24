# Handoff: qualm

Written 2026-09-22; updated the same day after the trials, again after
the MVP was built, on the night of 2026-09-23 (pop-up, focus sessions,
dashboard, 8-bit model, `serve` / `doctor`), and on the day of 2026-09-23
(check-ins replace daily budgets; then setup, Qualm.app and hosted Jev). This is the working
document: update the Status and Measured sections as you go. README.md is
the public face: what it is, screenshots, getting started.

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
  reason is built from the signal that fired, the margin over the threshold,
  and the page type and purpose; plus which part of the screen carried it
  (asked again with only the title, and with only the page text). Time2Stop
  (CHI 2024) found explanations raised the accuracy and receptivity of
  interventions.
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
  and the old file is kept as `rules.toml.bak`. New per-rule fields:
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
- **Take me back leaves the site** (reported by the user on Chrome: one
  Cmd-[ on 小红书 went from a note to the feed, which popped up again; and
  the dimmer doesn't take clicks, so they had browsed on meanwhile and the
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
- **Take me back closes the window, not the app** (reported by the user:
  a photo opened from a WeChat chat, a separate "Photos and Videos" window,
  stayed open and the chats came to the front). Outside browsers,
  `watcher.close_window()` presses the close button of the window the
  pop-up was about (matched by title and frame) when the app has others
  open; with one window, the app is hidden as before. Verified live on two
  Finder windows; not yet on WeChat's viewer itself.
- **Chats aren't social media; rules change through an agent** (user,
  same night: WeChat chats kept checking in as social, four "Not this one"
  didn't stop it, and "hand-written rules backfire a lot"). Measured on
  their 760 screens: WeChat chats scored 0.15-0.41 on social (threshold
  0.30). An exception in words ("chatting in WeChat, WhatsApp or iMessage
  is fine") pushed WhatsApp from ~0.17 to ~0.50; an allow class `chat`
  scored chats 0.56-0.81 and social pages 0.16 or less, so it's a starter
  now (live on their Mac). Three changes follow from it:
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
  the score alone (0.35-0.40: Slack's activity inbox, a claude.ai artifact,
  Qualm's old page) was wrong, and every right one came from a site pattern
  or feed_hit. Threshold 0.3 -> 0.5, plus weibo.com/ and m.weibo.cn/ as
  sites. Replaying the real log: 10 pop-ups on 9 pages -> 5 on 4, all
  Xiaohongshu feeds. Trials: precision 1.00 for Kev and Jev, recall Kev
  0.60 -> 0.67, Jev 0.67. Applied to the user's rules.toml too.
- **"Sensitive" audited**: 125 skipped of 1,933 judgements: 51 lock screen,
  4 Touch ID, 69 Chrome, 1 other. The Chrome ones had no content logged; by
  the pages before and after: checkouts, CMU/Duo sign-in, a password
  manager, mail, ad consoles; none entertainment. Private pages now keep
  their site (host only) and score, never title, text or screenshot.
- **The shift on real screens**: 250 of the user's own screens (messaging
  and mail left out) re-asked to Jev: the same decision as Kev on all 250;
  none taken for private; page kind agrees 86%, purpose 78%; 0.19 s p50.
- **Capture** (fork): Safari's AutoFill popover no longer leaks into later
  pages (the walk drops what it read before the page, and skips menus and
  popovers outside it); Chromium's empty first read is retried once per
  process (0.3 s); **OCR** (Vision, zh-Hans + en-US, accurate) when a
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
- **The 8-bit copy published** (the user's Hugging Face account, uploaded by
  the user: the permission classifier blocked me): RoderickQiu/kev-4b-mlx-8bit
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
  deleted). `rules.example.toml` and the server script (`kevserve.py`, was
  experiments/serve_capped.py) moved into the package; the root
  rules.example.toml is a symlink.
- **Kev as a managed dependency** (`localmodel.py`): its runtime (Kev pinned
  to commit 08ab0b8, PyTorch, MLX; 1.0 GB) goes into `kev-env/` via uv, on
  first use. The app starts the server when nothing answers on :8009 and
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
  Kev-4B's cache is built on its next start (not done here: the Mac had
  22 GB in swap and the live server running).
- **Memory in numbers, not "16 GB"**: the server holds 6.1-7.1 GB
  (measured); setup compares that with what's free now, counting swapped-out
  pages as in use. This Mac: 24 GB, ~33 GB in use (22 GB swapped out) ->
  "not enough: it would swap", hosted recommended.
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
  are off stay listed). *Open at login* only in Qualm.app. The watcher's
  answer cache is keyed by backend too, so a switch really asks the other
  model. Tested by building the real menu in a throwaway home and clicking
  through (hosted, a rule off, back, on).
- **Jev's cost, from real use** ($0.042 per million input tokens, output
  free; early-access pricing): a call is ~1,500 input tokens (5 rules, 2
  allow classes); the two logged days made 339 and 877 model calls over
  ~7-8 active hours: ~$0.02-0.06 a day, ~$0.50-1.25 a month. Each rule adds
  ~127 Jev tokens (measured). All of it, with Kev's memory, in docs/MODELS.md.

93 tests.

### MVP (built after the trials)

`qualm app` is a menu bar app (Python + PyObjC) that watches the
front window, and a floating panel that steps in with three answers: **Take me
back**, **I need it: 10 min** (asks why), and **Not this one** (teaches the rule an
exception). The policy follows docs/POLICY.md: block the mechanism (short
video, feeds, live rooms, compulsive checking), not the site; exempt learning,
work tools, search, sensitive pages and anything opened on purpose.

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
uv run qualm app                # menu bar + pop-up; starts the local model server itself (or the setup window, first run)
uv run python packaging/build_app.py   # dist/Qualm.app + dist/Qualm.dmg (packaging/README.md)

uv run qualm doctor             # everything checked, with fixes
uv run qualm serve              # the model server in this terminal: Kev-4B, 8-bit, :8009; --bits 16, --kev-dir DIR
uv run qualm rules list         # your rules; docs/PERSONALIZE.md for the rest
uv run qualm settings set backend=jev   # switch to hosted Jev (key: `qualm setup --backend jev`)
uv run qualm probe              # state only, no model; Ctrl-C to stop
uv run qualm ask --delay 3      # switch windows within 3 s, get one reading
uv run qualm label              # capture + label one moment -> data/labels.jsonl
uv run qualm eval               # precision/recall per threshold, latency
uv run qualm install            # start the app at every login (uninstall to undo)
uv run qualm app --demo         # show the panel once, learn nothing (deny|feed|checkin|timesup|focus|prompt)
uv run qualm focus write the report --minutes 50   # --stop ends it
uv run qualm pause 30           # --stop resumes
uv run qualm watch              # the same loop in the terminal, every judgement printed
open http://127.0.0.1:8765/     # dashboard (the app serves it; or `review --web`)
uv run qualm review             # the same judgements in the terminal
uv run qualm eval --reviews --suggest      # re-ask the model on everything you reviewed
uv run qualm export --lang en   # labels -> Kev training JSONL
```

Everything reads and writes `~/Library/Application Support/Qualm` (rules.toml,
data/, kev-env/, models/); logs in `~/Library/Logs/Qualm`.

Env knobs: `QUALM_HOME`, `QUALM_BACKEND=kev|jev` (over `[settings] backend`),
`QUALM_MODEL`, `QUALM_SHIFT` (log-odds; default 0 for Kev, 2 for Jev),
`KEV_URL`, `KEV_TIMEOUT` (default 30 s; the SDK's 10 s is shorter than 4B's
warm-up), `QUALM_QUESTION_STYLE=rule|direct` (see Measured).

Permissions: **Accessibility** for Qualm.app, or for the terminal when run
from a checkout. The AppleScript URL fallback may trigger an **Automation**
prompt per browser the first time.

## Layout

| File | What it does |
|---|---|
| `src/qualm/state.py` | Front window → `ScreenState` → compact `state` dict, cut to a character budget |
| `src/qualm/rules.py` | Rules from `rules.toml` (checked on load: fields, sites, `when`, regexes); builds the typed questions |
| `src/qualm/config.py` | Edits rules.toml in place, one key at a time; refuses a file that wouldn't load |
| `src/qualm/personalize.py` | The `rules` / `allow` / `except` / `never` / `settings` commands |
| `src/qualm/trial.py` | `rules test` / `label` / `tune`: a rule on your recent screens, and its threshold from your answers |
| `src/qualm/decide.py` | TypeSafe SDK client (Kev or Jev, from `[settings] backend`), the score shift, `ask()` |
| `src/qualm/paths.py` | Where things live: ~/Library/Application Support/Qualm, a checkout's legacy folder, the bundle, uv |
| `src/qualm/keychain.py` | The TypeSafe key in the login keychain (and .env / the environment first) |
| `src/qualm/localmodel.py` | The local model: runtime install, server command, the app's managed server, memory in numbers |
| `src/qualm/kevserve.py` | Kev's server as Qualm runs it (inside the runtime): MLX cache cap, 8-bit, weights saved once |
| `src/qualm/setup.py` | First run, shared by `qualm setup` and the window: migrate, recommend, apply |
| `src/qualm/onboard.py` | The setup window: a 5-page assistant (Auto Layout) |
| `packaging/` | `build_app.py` + `launcher.c`: Qualm.app and the DMG; README on signing |
| `src/qualm/policy.py` | Reading -> skip / allow / intervene: thresholds, URL patterns, exemptions, "opened on purpose", check-in sessions and their waits, snoozes, re-judging, user exceptions, the decision log |
| `src/qualm/watcher.py` | The loop shared by `watch` and `app`; "take me back" |
| `src/qualm/app.py` | Menu bar item (focus, pause), the intervention panel and the focus prompt (PyObjC); serves the dashboard |
| `src/qualm/ui.py` | The look: the blurred HUD panel, labels, badges, the screen dim |
| `src/qualm/explain.py` | The pop-up's words: headline, reason, the neutral context line, which part of the screen carried it |
| `src/qualm/review.py` | Dashboard server (http://127.0.0.1:8765): data, insights, verdicts, thresholds, exceptions, focus/pause, rule on/off |
| `src/qualm/dashboard.html` | The dashboard page: Today, Review, All screens, Insights, Rules |
| `src/qualm/doctor.py` | `qualm doctor` |
| `src/qualm/autostart.py` | `serve`, `install`, `uninstall`: the model server in a terminal, the app's LaunchAgent, the `qualm` shim |
| `src/qualm/cli.py` | `probe` / `ask` / `app` / `watch` / `label` / `harvest` / `eval` / `export`, plus the commands above |
| `src/qualm/rules.example.toml` | The starter rules and allow classes, in English, with measured thresholds and why in `note` |
| `docs/PERSONALIZE.md` | Every personalization command, the question limit, and the test-then-tune loop for a new rule |
| `.claude/skills/qualm/SKILL.md` | How Claude Code should drive the CLI for a user |
| `docs/MODELS.md` | On this Mac vs hosted: speed, memory, disk, Jev's usage and cost, from measurements |
| `docs/POLICY.md` | What to block on a desktop and what not, and how it generalizes and personalizes |
| `tests/test_policy.py` | The policy with made-up readings |
| `experiments/` | `demo_data.py` (made-up weeks for screenshots); trial tooling: `collect.py` + `manifest.py` (scripted pages, captured from a background Safari window via `bg.py`), `analyze.py` (per-rule threshold sweep and AUC over `eval --dump`), `state_tokens.py`, `serve_capped.py` |

`rules.toml` and `data/` are git-ignored: they contain what you read on screen.
In `data/`, the app keeps `judgements.jsonl` (every judgement: the capture,
exactly what the model read, every answer's probabilities, the screen before,
what the policy did and why; sensitive pages and unmonitored apps without
content), `shots/` (a ~900 px screenshot per new screen, none for sensitive
pages), `reviews.jsonl` (your answers on the review page),
`decisions.jsonl` (interventions, your answers, focus sessions), `session.json`
(pause and focus now), `reflections.jsonl` (the weekly question), `exceptions.jsonl` ("Not
this one", "Never here", `except`/`never` commands), `trials.jsonl` (`rules
test` scores, per exact rule wording) and `usage.json` (today's minutes and
check-in sessions per rule; `usage_history.json` keeps every day).

Never flagged: `[[allow]]` classes in rules.toml are kinds of page described
in words (shipped: shopping, "an online store: a product page, listing, cart
or checkout"); if the model says a page is one, no rule fires there except a
URL pattern. On the trial pages plus real CMU store pages, stores scored
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
   (`seenot-variant/app/src/main/java/com/seenot/app/ai/screen/ScreenAnalyzer.kt`,
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
pages loaded one by one in a background Safari window (Chrome's Focus Shield
extension blocks YouTube Shorts) and captured through the same Accessibility
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
  and p50 11.5 s around midnight. The trials measured 0.85 s. The
  difference was swap: 14.7 of 15.4 GB in use, the server's resident set
  down to 57 MB (its weights paged out).
- 20 pop-ups. True: Xiaohongshu explore (back), a Reddit thread (back
  twice). False, since fixed: WeChat and WhatsApp windows showing only a
  name (now "too little on screen"), a Chrome address-bar dropdown, Qualm's
  own review page, music.youtube.com (now an allow class), an Apple Ads
  page (feed_hit, now needs P(entertain) >= 0.6). Your call: claude.ai and
  linkedin.com ("Never here").
- 104 judgements skipped as sensitive: 60 Chrome pages, 40 the lock screen.
  Not audited: a false "sensitive" hides a page from every rule.

### 8-bit and 4-bit Kev-4B (2026-09-23, 119 trial pages, current rules)

`KEV_QUANT_BITS` in `serve_capped.py` quantizes after the LoRA merge
(`mlx.nn.quantize`, group 64; the pointer head stays fp32). Dumps in
data/runs/{bf16-en-now,q8-en,q4-en}.jsonl.

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
so thresholds carry over between backends.

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

0. **Move to Qualm.app** when convenient: quit the running copy (menu:
   Quit), open dist/Qualm.app (or drag it to Applications), turn on "Qualm"
   in System Settings > Privacy & Security > Accessibility. Its data folder
   is already the one in use. Earlier step, done the same day: quit the running `qualm app` (it
   predates check-ins and setup), then in the checkout: `uv run qualm setup`
   (copies rules.toml and data/ to Application Support, asks the four
   questions; on this Mac it recommends hosted, because of swap), then
   `uv run qualm app`, or open dist/Qualm.app (then grant it Accessibility).
   Done 2026-09-23 on this Mac: data copied home, the app runs from the
   checkout (nohup, log in ~/Library/Logs/Qualm/app.log) against the
   existing 8-bit server; readings 3-20 s from swap.
   With the local model, the first start builds Kev-4B's 8-bit cache: it
   loads bf16 once more (~11 GB peak), so stop the old server first. Not
   verified yet: Qualm.app opened from Finder, the permission prompt's name,
   the app's managed server on a real first start, a day of real use.
1. **Use the app for a few days, and review.** Browse normally, then go
   through the dashboard's Review tab: "Was Qualm right?" per card; for
   wrong ones, "Is this X?" per rule. Use the menu's "This should have been
   blocked" for misses as they happen. Try a focus session a day.
2. **Re-tune from your answers** on the Rules tab (suggested thresholds,
   Apply; exceptions in your own words), or `rules tune ID --apply`. When
   the wording of a rule is the problem: `rules set ID what="..."`, then
   `rules test ID` and `rules tune`. Not run yet: the CLI loop on a rule
   someone new writes from scratch, with real answers.
3. **Tell Jared about the 8-bit copy** (published 2026-09-23 as
   RoderickQiu/kev-4b-mlx-8bit, Apache-2.0, credited, marked unofficial):
   he might host it officially; and ask whether PyTorch can be optional for
   MLX serving (most of the 1 GB runtime). If Kev's checkpoint changes, a
   new copy must be published (Qualm checks provenance and builds locally
   meanwhile).
4. **Fine-tune only on a CUDA box or Modal**, once there are a few hundred of
   your own labels:
   `qualm export --lang en --labels data/labels.jsonl --out train.jsonl`, then
   `uv run python -m kev.train --data train.jsonl --init_from jaredpalmer/kev-4b --base Qwen/Qwen3.5-4B-Base --epochs 2 --lr 2e-5 --batch 1 --accum 8 --dtype bf16 --device cuda`.
   `--base` is required with `--init_from`; the default base is Qwen3-0.6B.
5. **Ship it:** Qualm.app exists (ad-hoc signed; opens and names itself "Qualm", verified); left: a Developer ID and
   notarization if it's to be shared widely, a license (none chosen yet), a
   public repository (the README is written for one).
## Known problems and gotchas

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
  one server, with `serve_capped.py`. Swapping shows up as multi-second latency
  and SDK timeouts, not as errors.
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
  title and nothing else; that's how a CMU store page scored 0.15 for stocks.
  Chrome's internal URLs are now skipped.
- **The running Kev server of 2026-09-22 was started from
  ~/Documents/seenot-desktop/experiments/serve_capped.py**, a path gone since
  the rename. It keeps running, but can't be restarted by that command: use
  `qualm serve`.
- **Headless Chrome screenshots of the dashboard don't exit** (the page
  refreshes every 15 s); the file is written anyway. /tmp/shoot.py-style:
  a timeout of 30 s per shot.
- **Harvested labels cover one rule each** (the rule that fired), and only hits
  the panel showed. Misses need `label`.

## Background and sources

- SeeNot Android source: `~/Documents/seenot-variant`. See `ConstraintEnums.kt`
  (DENY / TIME_CAP / NO_MONITOR) and `ScreenAnalyzer.kt`.
- Offline eval from the SUSTech thesis: WeChat precision went from 79.3% to
  93.0% after 17 repair rules; Taobao from 75.9% to 98.4%. That dataset is
  Android screenshots and does not transfer; desktop needs new labels.
- Jev: https://typesafe.ai/blog/introducing-system-one-models-and-jev
- Jev API (Python SDK `typesafe-sdk`, installed at 0.7.1): https://dev.to/valyuai/how-to-use-jev-a-practical-guide-to-typesafes-system-one-model-g5e
- Kev: https://github.com/jaredpalmer/kev (MLX backend: PR #43)
