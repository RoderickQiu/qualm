# Handoff: seenot-desktop

Written 2026-09-22; updated the same day after the trials, and again after
the MVP was built. This is the working
document: update the Status and Measured sections as you go.

## What this is

A macOS version of SeeNot. SeeNot's model call is already a typed decision:
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
- **Jev**, hosted: set `SEENOT_BACKEND=jev` and `TYPESAFE_API_KEY`. There is no
  key on this machine yet; early access is waitlisted at console.typesafe.ai.

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
- **Never judge SeeNot**: a running copy had judged a demo panel, and the review
  page opened inside Cursor (a `vscode-file://` URL) had popped up as stocks.
  SeeNot now skips its own panel and any window titled "SeeNot review".
- **Work tools are left alone** whenever the model's best guess is "work", not
  only when it's 60% sure: code and notes are full of words any rule matches.
- **Stocks removed from the shipped rules**: a niche habit that, in real use,
  fired on shopping chats and on SeeNot's own review page. The trial numbers
  below still include it.
- **[[allow]] classes, never here, thin screens, Chrome capture**: see below.

### MVP (built after the trials)

`seenot-desktop app` is a menu bar app (Python + PyObjC) that watches the
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
  "SeeNot" item wasn't visible in a screenshot with a full menu bar and Thaw
  running.

Verified working on this Mac (M5 Pro, 24 GB, macOS 27): `probe`, `ask`, `watch`,
`eval`, `export`, `app --demo`. Not exercised: `label` and `harvest` (they need you).

Not built:
- OCR fallback for apps whose text isn't in the Accessibility tree (video
  players, canvases).
- Event-driven triggers (currently a 0.5 s poll of the front window; the model is
  asked only when the screen changes, or its text changes, at most every 30 s).
- A signed .app bundle: the menu bar item still shows as "python3" in menu bar
  managers.

## Run it

```bash
# 1. Model server (Kev is cloned at ~/Documents/kev, commit 08ab0b8)
cd ~/Documents/kev
KEV_DTYPE=bf16 uv run --extra serve python ~/Documents/seenot-desktop/experiments/serve_capped.py \
    --run jaredpalmer/kev-4b --port 8009
#    serve_capped.py = kev.serve with MLX's buffer cache capped at 1 GB. Plain
#    `python -m kev.serve` holds 17 GB for 4B and swaps a 24 GB Mac.
#    Kev-0.8B: --run jaredpalmer/kev-0.8b (fast, but too weak; see Measured)
#    Run one server at a time.

# 2. This repo
cd ~/Documents/seenot-desktop
cp rules.example.toml rules.toml         # already done once; edit freely
uv run seenot-desktop probe              # state only, no model; Ctrl-C to stop
uv run seenot-desktop ask --delay 3      # switch windows within 3 s, get one reading
uv run seenot-desktop label              # capture + label one moment -> data/labels.jsonl
uv run seenot-desktop eval --lang zh     # precision/recall per threshold, latency
uv run seenot-desktop eval --lang en
uv run seenot-desktop install            # or: start model server + app at every login (uninstall to undo)
uv run seenot-desktop app                # the MVP: menu bar + intervention panel
uv run seenot-desktop app --demo         # show the panel once, learn nothing
uv run seenot-desktop watch              # the same loop in the terminal, every judgement printed
open http://127.0.0.1:8765/              # review page (the app serves it; or `review --web`)
uv run seenot-desktop review             # the same judgements in the terminal
uv run seenot-desktop review --fix <id> stocks=no   # answer one from the terminal
uv run seenot-desktop eval --reviews --suggest      # re-ask the model on everything you reviewed
uv run seenot-desktop harvest            # your answers to the panel -> data/labels.jsonl
uv run seenot-desktop eval --suggest     # thresholds from your labels, for rules.toml
uv run seenot-desktop export --lang en   # labels -> Kev training JSONL (data/train.jsonl)
```

Env knobs: `KEV_URL`, `KEV_TIMEOUT` (default 30 s; the SDK's 10 s is shorter
than 4B's warm-up), `SEENOT_QUESTION_STYLE=rule|direct` (see Measured).

Permissions: the terminal app needs **Accessibility** (System Settings →
Privacy & Security). It already has it on this Mac. The AppleScript URL
fallback may trigger an **Automation** prompt per browser the first time.

## Layout

| File | What it does |
|---|---|
| `src/seenot_desktop/state.py` | Front window → `ScreenState` → compact `state` dict, cut to a character budget |
| `src/seenot_desktop/rules.py` | Rules from `rules.toml`; builds the typed questions |
| `src/seenot_desktop/decide.py` | TypeSafe SDK client (Kev or Jev) and `ask()` |
| `src/seenot_desktop/policy.py` | Reading -> skip / allow / count / intervene: thresholds, URL patterns, exemptions, "opened on purpose", budgets, snoozes, user exceptions, the decision log |
| `src/seenot_desktop/watcher.py` | The loop shared by `watch` and `app`; "take me back" |
| `src/seenot_desktop/app.py` | Menu bar item and intervention panel (PyObjC); serves the review page |
| `src/seenot_desktop/review.py` | Review page (http://127.0.0.1:8765): your verdicts, threshold suggestions from them, applying thresholds and exceptions |
| `src/seenot_desktop/cli.py` | `probe` / `ask` / `app` / `watch` / `label` / `harvest` / `eval` / `export` |
| `rules.example.toml` | Six default rules, each in Chinese and English, with measured thresholds |
| `docs/POLICY.md` | What to block on a desktop and what not, and how it generalizes and personalizes |
| `tests/test_policy.py` | The policy with made-up readings |
| `experiments/` | Trial tooling: `collect.py` + `manifest.py` (scripted pages, captured from a background Safari window via `bg.py`), `analyze.py` (per-rule threshold sweep and AUC over `eval --dump`), `state_tokens.py`, `serve_capped.py` |

`rules.toml` and `data/` are git-ignored: they contain what you read on screen.
In `data/`, the app keeps `judgements.jsonl` (every judgement: the capture,
exactly what the model read, every answer's probabilities, the screen before,
what the policy did and why; sensitive pages and unmonitored apps without
content), `shots/` (a ~900 px screenshot per new screen, none for sensitive
pages), `reviews.jsonl` (your answers on the review page),
`decisions.jsonl` (interventions and your answers), `exceptions.jsonl` ("Not
this one") and `usage.json` (today's budgets).

Never flagged: `[[allow]]` classes in rules.toml are kinds of page described
in words (shipped: shopping, "an online store: a product page, listing, cart
or checkout"); if the model says a page is one, no rule fires there except a
URL pattern. On the trial pages plus real CMU store pages, stores scored
0.42-0.92 and every other page 0.18 or less, hence threshold 0.35.

The stocks threshold went from 0.13 to 0.3 after real use: WhatsApp chats
about shopping scored up to 0.27, while trial stock pages mostly score
0.27-0.69. Stock forums (Guba, r/wallstreetbets, Stocktwits, Xueqiu), which
the model scores low, are URL patterns instead. Whole policy on the trial
set: precision 0.96, recall 1.00 (was 0.96 / 0.92). The pop-up's
"Never in <app>" / "Never on <site>" does the same for one app or site.

Testing mode: `[settings] budgets = false` (the current default) makes every
rule hit pop up at once, time caps included. Set it to true for real budgets.

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
     for TIME_CAP rules.

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

"direct" (`SEENOT_QUESTION_STYLE=direct`) asks "Is the open content X?"
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

1. **Use the app for a few days, and review.** Run `seenot-desktop app`
   (Kev-4B server up first), browse normally, then go through the review page:
   "Was SeeNot right?" per card; for wrong ones, "Is this X?" per rule. Use
   the menu's "This should have been blocked" for misses as they happen.
2. **Re-tune from your answers** on the same page (suggested thresholds, Apply;
   exceptions in your own words). The app reloads rules.toml and exceptions
   without a restart. When the wording of a rule is the problem, edit its
   description in rules.toml.
3. **Fix what the trials showed is weak:**
   - Feeds on sites that look like single items (X profiles, Guba lists).
4. **Fine-tune only on a CUDA box or Modal**, once there are a few hundred of
   your own labels:
   `seenot-desktop export --lang en --labels data/labels.jsonl --out train.jsonl`, then
   `uv run python -m kev.train --data train.jsonl --init_from jaredpalmer/kev-4b --base Qwen/Qwen3.5-4B-Base --epochs 2 --lr 2e-5 --batch 1 --accum 8 --dtype bf16 --device cuda`.
   `--base` is required with `--init_from`; the default base is Qwen3-0.6B.
5. **Make the watcher cheaper and wider:**
   - Replace polling with an `AXObserver` (focused-window and title-changed
     notifications) plus `NSWorkspace.didActivateApplicationNotification`.
   - Add a Vision OCR fallback (`VNRecognizeTextRequest`, zh-Hans) when the
     Accessibility tree yields no text.
6. **Ship it:** a signed `SeeNot.app` (Swift `MenuBarExtra`, or py2app) with its own
   name and permissions; `install` covers start-at-login meanwhile. Port
   SeeNot's session intents: "I'm here to do X for 20 minutes" before a
   session, in place of the per-rule snooze.
7. **Graded friction past the pop-up** (InteractOut, CHI 2024: slowing
   interaction beat lockouts): dim or blur the window when you keep going
   back to a page after "Take me back".

## Known problems and gotchas

- **Chromium/Electron** only build the web Accessibility tree after a client
  sets `AXManualAccessibility`. `capture()` does this every time. The set call
  itself returns -25205, but the tree still appears, so a very first capture
  may come back without page text.
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
- **Safari's AutoFill popover leaks into later captures.** After a sign-in page,
  "Apple Account / Continue with Touch ID / <your name>" stayed in the window's
  AX tree for the next 11 pages, which makes ordinary pages look like login
  pages. Stripped from the trial data; `capture()` doesn't filter it yet.
- **Background Safari windows in Stage Manager have no web AX tree.** Only
  matters for `experiments/collect.py`, which needs the Safari window visible
  (not parked in the Stage Manager strip). The watcher reads the front window,
  which is always rendered.
- **Chrome's address-bar dropdown is a web area too.** `capture()` used to
  take it for the page (URL `chrome://omnibox-popup...`), so the model saw a
  title and nothing else; that's how a CMU store page scored 0.15 for stocks.
  Chrome's internal URLs are now skipped.
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
