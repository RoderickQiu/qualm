# Handoff: seenot-desktop

Written 2026-09-22, updated the same day after the trial session. This is the working
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

Verified working on this Mac (M5 Pro, 24 GB, macOS 27): `probe`, `ask`, `eval`, `export`.
Still not exercised: `label` (interactive), `watch` with tuned thresholds.

Not built:
- OCR fallback for apps whose text isn't in the Accessibility tree (video
  players, canvases).
- Event-driven triggers (currently a 0.5 s poll on the window signature).
- The Swift menu bar app.

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
uv run seenot-desktop watch              # the live loop, with macOS notifications
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
| `src/seenot_desktop/decide.py` | TypeSafe SDK client (Kev or Jev), `ask()`, and the `Gate` that turns answers into actions |
| `src/seenot_desktop/cli.py` | `probe` / `ask` / `watch` / `label` / `eval` |
| `rules.example.toml` | Three sample rules, each in Chinese and English |
| `experiments/` | Trial tooling: `collect.py` + `manifest.py` (scripted pages, captured from a background Safari window via `bg.py`), `analyze.py` (per-rule threshold sweep and AUC over `eval --dump`), `state_tokens.py`, `serve_capped.py` |

`rules.toml` and `data/` are git-ignored: they contain what you read on screen.

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
4. **Act on probabilities, not on the chosen answer.**
   - `p_hit` ≥ 0.85: intervene.
   - 0.5 ≤ `p_hit` < 0.85 on two readings in a row: nudge.
   - A sensitive page: skip that reading, and forget the previous one so a
     pending nudge doesn't carry over.

   The thresholds are placeholders until `eval` gives real numbers.
5. **Repair rules become `exceptions`** in each rule's question text. That's
   the desktop version of SeeNot's false-positive → repair-rule loop
   (`FalsePositiveRuleGenerator.kt`). Not yet generated automatically.
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

1. **Recalibrate before `watch` is usable.** Pick one:
   - Per-rule thresholds in `rules.toml` (e.g. `threshold = 0.15`), read
     from `eval`. Cheapest; do this first.
   - Or score `p_hit / (p_hit + p_safe)`, which ignores `unknown` mass.
   - Or fine-tune (step 4), which fixes calibration properly.
2. **Label real moments with `seenot-desktop label`** (about 100 across a
   normal, logged-in day) and re-run `eval --lang en` on 4B. The trial set is
   logged-out and URL-obvious; real screens will be harder. If precision >= 0.9
   still holds with per-rule thresholds, keep the setup.
3. **Livestream signal:** add "live" cues to the state (the AX tree often has a
   LIVE badge or viewer count), or add an exception/example to the rule text,
   then re-test the Twitch pages.
4. **Fine-tune only on a CUDA box or Modal**, not this Mac:
   `seenot-desktop export --lang en --labels data/labels.jsonl --out train.jsonl`, then
   `uv run python -m kev.train --data train.jsonl --init_from jaredpalmer/kev-4b --base Qwen/Qwen3.5-4B-Base --epochs 2 --lr 2e-5 --batch 1 --accum 8 --dtype bf16 --device cuda`.
   `--base` is required with `--init_from`; the default base is Qwen3-0.6B.
   `export` output loads with `kev.data.load_records` (checked); 2 of 119
   records exceed the training context and get dropped.
5. **Make the watcher cheaper and wider:**
   - Replace polling with an `AXObserver` (focused-window and title-changed
     notifications) plus `NSWorkspace.didActivateApplicationNotification`.
   - Add a Vision OCR fallback (`VNRecognizeTextRequest`, zh-Hans) when the
     Accessibility tree yields no text.
6. **The app:** a Swift `MenuBarExtra` with a floating `NSPanel` for
   interventions, talking to the local Kev server. Port SeeNot's session
   intents and the three constraint types.

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

## Background and sources

- SeeNot Android source: `~/Documents/seenot-variant`. See `ConstraintEnums.kt`
  (DENY / TIME_CAP / NO_MONITOR) and `ScreenAnalyzer.kt`.
- Offline eval from the SUSTech thesis: WeChat precision went from 79.3% to
  93.0% after 17 repair rules; Taobao from 75.9% to 98.4%. That dataset is
  Android screenshots and does not transfer; desktop needs new labels.
- Jev: https://typesafe.ai/blog/introducing-system-one-models-and-jev
- Jev API (Python SDK `typesafe-sdk`, installed at 0.7.1): https://dev.to/valyuai/how-to-use-jev-a-practical-guide-to-typesafes-system-one-model-g5e
- Kev: https://github.com/jaredpalmer/kev (MLX backend: PR #43)
