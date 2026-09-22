# Handoff: seenot-desktop

Written 2026-09-22, at the end of the setup session. This is the working
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

Verified working on this Mac (M5 Pro, 24 GB, macOS 27):
- `probe`: reads the app, window title, URL, headings and visible text from
  Chrome in about 0.3 s, including process startup.
- `ask`: sends that state to local Kev-0.8B and prints every answer. The first
  call took 4.6 s (warm-up); after that it took 72 ms.

Written but **not yet exercised**:
- `label` (interactive; needs you at the keyboard)
- `eval` (needs labels)
- `watch` (the loop runs, but the gate thresholds are untuned)

Not built:
- OCR fallback for apps whose text isn't in the Accessibility tree (video
  players, canvases).
- Event-driven triggers (currently a 0.5 s poll on the window signature).
- The Swift menu bar app.

## Run it

```bash
# 1. Model server (Kev is cloned at ~/Documents/kev, commit 08ab0b8)
cd ~/Documents/kev
KEV_DTYPE=bf16 uv run --extra serve python -m kev.serve --run jaredpalmer/kev-0.8b --port 8009
#    Kev-4B instead: --run jaredpalmer/kev-4b (about 9 GB; not downloaded yet)

# 2. This repo
cd ~/Documents/seenot-desktop
cp rules.example.toml rules.toml         # already done once; edit freely
uv run seenot-desktop probe              # state only, no model; Ctrl-C to stop
uv run seenot-desktop ask --delay 3      # switch windows within 3 s, get one reading
uv run seenot-desktop label              # capture + label one moment -> data/labels.jsonl
uv run seenot-desktop eval --lang zh     # precision/recall per threshold, latency
uv run seenot-desktop eval --lang en
uv run seenot-desktop watch              # the live loop, with macOS notifications
```

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

## Measured so far

| What | Number | Notes |
|---|---|---|
| Screen capture (Chrome, Outlook page) | ~0.3 s wall | includes `uv run` startup; the capture alone is less |
| Kev-0.8B, first call | 4,639 ms | warm-up |
| Kev-0.8B, next call | 72 ms | 5 questions, same state |
| Input tokens per call | ~800 | state **plus** questions; the state alone is unmeasured, so it's unclear whether it stays under 384 |

One real reading, of an Outlook inbox open on one email:
- page_kind: `feed` at 0.41. An open email in an inbox is ambiguous, so this
  is a labeling question too.
- sensitive: 0.27.
- All three sample rules: safe or unknown, with `p_hit` at most 0.21.

Correct in direction, but one reading proves nothing.

A synthetic smoke test (two hand-made records, only to exercise `eval`):
- A YouTube Shorts page titled 「抖音 热门短视频合集」 should be an obvious
  hit for `shortvideo`, yet 0.8B put `p_hit` only between 0.5 and 0.7. At the
  0.85 threshold it would be missed.
- Warm latency: p50 66 ms, p95 78 ms.

Expect 0.8B to be too weak to use as is; Kev-4B is the real candidate.

## Next steps, in order

1. **Label about 100 moments.** Run `seenot-desktop label` across a normal day.
   Aim for at least 20 true violations per DENY rule, or precision will rest on
   a handful of cases. Mix in feeds, single items, work apps and a few login
   or payment pages.
2. **Run the language test first; it decides the route.** Kev's training data
   (`decision-v7`) is English, and TypeSafe hasn't said whether Jev handles
   Chinese. Compare `eval --lang zh` with `eval --lang en`, then repeat on
   Kev-4B. Rule to decide by: if some setup reaches precision ≥ 0.9 at a
   threshold with usable recall, keep it and move to step 4. If none does, go
   to step 3.
3. **Fine-tune Kev on your labels** (only if step 2 fails):
   ```bash
   uv run python -m kev.train --data train.jsonl --init_from jaredpalmer/kev-4b \
       --epochs 2 --lr 2e-5 --batch 1 --accum 8 --dtype bf16 --device cuda
   ```
   - Kev's JSONL format is `{"state": ..., "questions": {name: {"type", "criteria", "label"}}}`.
     A converter from `data/labels.jsonl` is not written yet.
   - Their recipe assumes CUDA. 0.8B trains on a 4 GB GPU; whether it trains on
     this Mac (MPS) is unverified. A rented GPU or Modal (`modal_app.py` in the
     Kev repo) is the fallback.
4. **Make the watcher cheaper and wider:**
   - Replace polling with an `AXObserver` (focused-window and title-changed
     notifications) plus `NSWorkspace.didActivateApplicationNotification`.
   - Add a Vision OCR fallback (`VNRecognizeTextRequest`, zh-Hans) when the
     Accessibility tree yields no text.
5. **The app:** a Swift `MenuBarExtra` with a floating `NSPanel` for
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
- **`input_tokens` counts questions too.** Measure the state's own size before
  concluding anything about the 384-token limit.

## Background and sources

- SeeNot Android source: `~/Documents/seenot-variant`. See `ConstraintEnums.kt`
  (DENY / TIME_CAP / NO_MONITOR) and `ScreenAnalyzer.kt`.
- Offline eval from the SUSTech thesis: WeChat precision went from 79.3% to
  93.0% after 17 repair rules; Taobao from 75.9% to 98.4%. That dataset is
  Android screenshots and does not transfer; desktop needs new labels.
- Jev: https://typesafe.ai/blog/introducing-system-one-models-and-jev
- Jev API (Python SDK `typesafe-sdk`, installed at 0.7.1): https://dev.to/valyuai/how-to-use-jev-a-practical-guide-to-typesafes-system-one-model-g5e
- Kev: https://github.com/jaredpalmer/kev (MLX backend: PR #43)
