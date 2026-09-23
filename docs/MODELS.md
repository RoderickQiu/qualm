# The model: on this Mac, or hosted

Qualm asks a small model what's on screen each time the screen changes. That
model runs in one of two places, chosen in setup and switchable from the menu
bar (Model):

- **On this Mac**: Kev-4B, an open-source Jev-style model, quantized to 8 bits
  and run with MLX. Nothing leaves the Mac.
- **Hosted**: TypeSafe's Jev 1.13. The text of each new screen is sent to
  TypeSafe.

Both answer the same questions, and the same rules work for both: Jev's
scores are shifted onto Kev's scale (`decide.SHIFT`). Every number below was
measured on a Mac with an M5 Pro and 24 GB, running macOS 27, in September
2026, unless it says otherwise.

## At a glance

| | On this Mac (Kev-4B, 8-bit) | Hosted (Jev 1.13) |
|---|---|---|
| Time per reading | ~1.1 s with memory to spare (p95 1.6 s); 3–20 s when the Mac is swapping | ~0.2 s (p90 0.8 s) |
| Memory | 6.1–7.1 GB for the model server, plus 88 MB for the app | 88 MB for the app |
| Disk | ~14 GB: runtime 1.1 GB, weights 9 GB, 8-bit copy 4.2 GB | none |
| Money | none | ~$0.02–0.06 per workday (see below) |
| Privacy | nothing leaves the Mac | the text of each new screen goes to TypeSafe |
| Accuracy (119 trial pages, the whole policy) | the reference | the same or better on every rule |

## Hosted: how much it's used, and what it costs

### When Qualm calls the model

Qualm doesn't call the model on a timer. It calls when:

- a new screen has been stable for 0.5 s;
- the text of the same screen changes, at most once every 30 s;
- a pop-up opens: 2 smaller calls to explain it (the title alone, the page text alone).

An answer is cached by exactly what the model read, so going back to a tab
you've seen doesn't call it again. Before the cache, 92% of calls re-asked
text the model had already read. Pages Qualm decides without the model make
no call at all: your own sites and URL patterns, allowed sites, "Never
here", private pages, apps that aren't monitored.

### Calls per day, from real use

From Qualm's own log (data/judgements.jsonl), the two days it ran:

| Day | Active | Judgements | Model calls | Cached | Without the model |
|---|---|---|---|---|---|
| 2026-09-22 | ~7.8 h | 1,193 | 877 | 137 | 179 |
| 2026-09-23 | ~6.8 h | 686 | 339 | 230 | 117 |

The first day ran mostly before the answer cache was in place, so the second
day is the better guide: **about 340 calls in a workday, ~50 an hour**. The
busiest hour had 152.

### Tokens per call

Jev charges by input tokens: the screen, as Qualm cuts it (app, title, URL,
headings and some text, up to 700 characters), plus the questions. Measured
on 5 trial pages, with the 2 allow classes and 3 shared questions always
asked:

| Rules on | Input tokens per call |
|---|---|
| 1 | ~950 |
| 3 | ~1,225 |
| 5 (the starters) | ~1,450 |
| 10 | ~2,090 (extrapolated) |

Each rule adds about **127 tokens**. A screen with a single question is about
490 tokens. Output tokens (the answers) are free.

### Cost

At TypeSafe's current early-access price of **$0.042 per million input
tokens**:

| | Calls | Input tokens | Cost |
|---|---|---|---|
| A typical workday, 5 rules | ~340 | ~0.5 M | ~$0.02 |
| A heavy day, 5 rules | ~880 | ~1.3 M | ~$0.06 |
| A typical workday, 10 rules | ~340 | ~0.7 M | ~$0.03 |
| A month, 22 workdays, 5 rules | | | ~$0.50–1.25 |

Explaining pop-ups adds 2 calls each, so 20 pop-ups a day is under a tenth
of a cent. The cost formula is calls × tokens per call × price.

**Caveats:**
- The price is early-access, not a published rate card, and may change. One
  unofficial site lists $0.42 per million, ten times more; at that price a
  month is about $5–12.
- Rate limits are 1,200 requests a minute. Qualm's busiest hour made about 2
  a minute.

Sources: [OpenRouter: Jev 1.13](https://openrouter.ai/typesafe/jev-1.13),
[MarkTechPost: TypeSafe releases Jev](https://www.marktechpost.com/2026/09/19/typesafe-ai-releases-jev/),
[Crescent AI: Jev pricing](https://www.ai-crescent.com/blog/jev-typesafe-pricing-2026),
and the outlier, [jevtypesafeai.com/pricing](https://jevtypesafeai.com/pricing).

## On this Mac: how much memory Kev uses

### The server

The model server is a separate process (`kevserve.py`, in the runtime at
`~/Library/Application Support/Qualm/kev-env`). Its footprint after answering
the 119 trial pages:

| Kev-4B weights | Memory | Answers |
|---|---|---|
| bf16 (as trained) | 9.9–11 GB | the reference |
| **8-bit (the default)** | **6.1–7.1 GB** | the same: equal AUC on every rule, the same page kind on all 119 pages, scores within 0.04 |
| 4-bit | 4.1 GB | worse: social recall fell from 0.95 to 0.85; not offered |

Roughly 4.2 GB of the 8-bit footprint is the weights. The rest is MLX's
buffers, capped at 1 GB (`MLX_CACHE_GB`), and the Python around it. Without
the cap, the bf16 server grew to 17 GB and swapped a 24 GB Mac.

### What memory it needs

What matters is whether the Mac can spare the memory next to what's already
running, not its total RAM. Setup and `qualm doctor` count what apps hold
now, in RAM or swapped out, and recommend the local model only if 7.1 GB plus
2 GB of headroom is free.

On 2026-09-23 this Mac had 24 GB and about 37 GB in use (22 GB of it swapped
out), so the answer was "not enough: it would swap". The running server showed
why that matters:

- It held 6.0 GB, but only 20 MB of it was in RAM; the rest was swapped out.
- Its peak since it started was 17 GB.
- Readings took 3–20 s, against ~1 s with memory to spare, and one timed out
  at 30 s.
- On the first day of real use it was the same: p50 1.8 s and p95 8.5 s,
  rising to 11.5 s around midnight.

### Starting it

The first start downloads Kev-4B's adapter (0.3 GB) and its Qwen3.5-4B base
(8.7 GB). It then merges the adapter, quantizes to 8 bits, and saves that copy
in `models/` (4.2 GB). Every later start loads the saved copy. Measured on
this Mac on 2026-09-23, with 22 GB already in swap, by the app's own managed
server:

| Start | Ready after | Memory peak | Then |
|---|---|---|---|
| First (build the 8-bit copy) | 100 s | 16 GB | 6–7 GB |
| Every later one (the saved copy) | 11 s | 4.8 GB | ~5–7 GB |

So the saved copy matters: a start no longer needs 16 GB for a minute and a
half. On Kev-0.8B the effect was small (peak 3.6 → 3.4 GB), because its bf16
weights are small next to the Python around them. Answers from the saved copy
are identical to freshly quantized ones (max difference 0 on 15 pages × 11
questions).

A fresh server also answers faster on a swapped Mac: readings went from 3–20 s
(the old server, mostly swapped out) to 2–3 s right after the restart.

The app starts the server when nothing answers on :8009, and starts it again
within ~5 s if it stops or crashes. Quit (in the menu) stops it too. A server
from `qualm serve` is used and left alone.

### Questions per reading

Kev answers every question in one pass, so up to about 10 rules costs about
the same as 1: 0.4 s for 1 rule, 0.6 s for 10. Past that, one long pass grows
faster than linearly: 1.8 s at 25 rules, 7.9 s at 50, and a timeout (and
14 GB of swap) at 100. Hence the limit of 25 questions a reading
(`[settings] max_questions`). The details are in HANDOFF.md, Measured.
