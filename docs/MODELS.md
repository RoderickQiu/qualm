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
| Disk | ~6 GB: runtime 1.1 GB, adapter 0.3 GB, 8-bit copy 4.5 GB | none |
| Money | none | ~$0.02–0.06 per workday, about $1 a month (see below) |
| Needs | Apple silicon, macOS 14 or later, 6–7 GB of memory to spare | a TypeSafe account and API key; macOS 13 or later |
| Privacy | nothing leaves the Mac | the text of each new screen goes to TypeSafe |
| Accuracy (119 trial pages, the whole policy) | the reference | the same or better on every rule |
| Pages outside the trial set | pops up where Jev doesn't, or the other way round, on ~1 page in 10 (below) | the same, seen from Jev |

## Where the two disagree

The score shift was fitted on the 119 trial pages, and on 250 of the author's
own screens the two models made the same decision every time. Pages further
from that set disagree more. On 53 made-up pages the trials never had (in
English, Chinese, Japanese and German, with the starter rules plus five a new
user added: news, shopping, games, sports, dating), pop-up or not differed on
5, and a different rule fired on 7 more. Most of it is one answer, whether a
page is a feed or a single item, because a rule about content never fires on
a feed:

| Page | Kev-4B | Jev |
|---|---|---|
| ESPN scoreboard | a single item: the sports rule pops up | a feed: nothing |
| Gmail inbox | a single item: a shopping rule pops up (wrongly) | a feed: nothing |
| Hacker News | a feed for entertainment (0.64): feeds pops up | a feed for work: nothing |
| Tinder | a feed for entertainment (0.70): feeds pops up | nothing |
| Facebook Watch | a single item: a social check-in (the starters now list its address, so feeds pops up on both) | a feed: feeds pops up |

With the starter rules as they ship now, on 40 made-up and trial pages scored
by both, pop-up or not agreed on 38 and the same pop-up showed on 34 (a
recorded let's-play read as a livestream by one and a video by the other, a
Twitch recording the other way round).

So switching the model can change what happens on pages nobody measured.
`rules test` scores with the model in use, and its scores are kept per model,
so after a switch it shows what the other model does on your own screens.

## Hosted: how much it's used, and what it costs

### When Qualm calls the model

Qualm doesn't call the model on a timer. It calls when:

- a new screen has been stable for 0.5 s;
- the text of the same screen changes, at most once every 30 s;
- a pop-up opens: 2 smaller calls to explain it (the title alone, the page text alone), except
  where a rule's own site or app made it pop up, since the model's say didn't count there.

An answer is cached by exactly what the model read, so going back to a tab
you've seen doesn't call it again. Before the cache, 92% of calls re-asked
text the model had already read. Screens Qualm decides without the model make
no call at all: allowed sites, "Never here", the sites and apps an allow class
lists (music players) unless a rule names that very site or app or steps in
on that class, an app when every rule that's on names it, apps that aren't
monitored, and everything while paused. Hosted, nothing at all is sent while
rules.toml doesn't load: the file may name apps to keep private in a way
Qualm can't read. In an app some rules name, the other rules are asked as
anywhere else. A rule's own sites are still read (a login page
there is still left alone as private); otherwise they step in whatever the
model scores, and even while it's down, when nothing can be found private.
So the starters list no home page that is a login form when logged out
(Facebook's and Instagram's are left to the model).

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

The first start installs the runtime (about 1 GB: Python, PyTorch, MLX and
Kev, from an archive of the pinned Kev commit, so no git is needed) and
downloads Kev-4B's adapter (0.3 GB) and the 8-bit copy, already merged and
quantized:
[RoderickQiu/kev-4b-mlx-8bit](https://huggingface.co/RoderickQiu/kev-4b-mlx-8bit)
(4.5 GB, Apache-2.0, published 2026-09-23): about 6 GB in all. Kev-4B, its
Qwen3.5 base and the copy are pinned to the revisions the copy was built from
(`localmodel.MODEL`, `BASE`, `PREBUILT`), so a new upstream commit changes
nothing until the pins move. The copy is used only if its `provenance.json`
names the same checkpoint, base and 8-bit settings, and its SHA-256 and size
match the ones pinned next to it (`PREBUILT_SHA256`, `PREBUILT_BYTES`): a
file of another size is refused before a byte of it is kept, and one with
another hash after it downloads. A mirror that serves another file reads
"<host> serves the wrong model file: set another mirror" in the menu, and
Qualm doesn't download from it again until `hf_endpoint` or HF_ENDPOINT
changes (it looks every 30 s). Without a mirror, a wrong file (a proxy, a
Wi-Fi sign-in page) is tried again with the usual waits, since that often
passes.
Qualm builds it here (downloading the Qwen3.5-4B base, 8.7 GB, then merging
and quantizing) only if the copy can't be had and the Mac has 16 GB of
memory and 14 GB of disk to spare; any other failure stops the start with a
reason. Every later start loads the saved copy from `models/`, and when a new
pinned copy lands, the older copies of Kev-4B there are removed (only copies
Qualm made, marked with a `qualm.json`, or an unfinished download: a
`qualm serve --model` build of yours stays).

How long the first start takes depends on the connection. The menu bar icon
is an arrow while it downloads and an hourglass while it loads, the menu
shows the GB so far, a download that gets cut off goes on where it stopped,
and Model > Hosted or Quit stops it (so does switching to hosted from a
terminal or an agent, `qualm settings set backend=jev`, within about 5 s).
Where huggingface.co is blocked, set a mirror
(`qualm settings set hf_endpoint=https://hf-mirror.com`, an https address;
HF_ENDPOINT in the environment wins); a mirror that can't be reached is
named in the menu. Mirrors for the runtime's Python packages go in
`~/.config/uv/uv.toml` (`index-url`, `python-install-mirror`). Nothing
there redirects the Kev archive the runtime is installed from, which is on
GitHub: where GitHub is blocked, run
`QUALM_KEV_ARCHIVE=<a copy of https://github.com/jaredpalmer/kev/archive/08ab0b87d27cb5577a3b371ad7ed4e4686b0502b.zip> qualm serve`
once in a terminal; the runtime goes into the same Qualm folder, and the app
uses it from then on. Measured on this Mac on 2026-09-23, with 22 GB
already in swap:

| Start | Ready after | Memory peak | Then |
|---|---|---|---|
| First, with the published copy | 191 s (mostly the download, ~25 MB/s) | 4.8 GB | ~5–7 GB |
| First, building it here | 100 s (plus 9 GB of downloads) | 16 GB | 6–7 GB |
| Every later one (the saved copy) | 11 s | 4.8 GB | ~5–7 GB |

The published copy answers exactly as a locally built one: the same file (by
SHA-256), and a difference of 0.0 on 15 pages × 10 questions against the
running server.

So the saved copy matters: a start no longer needs 16 GB for a minute and a
half. On Kev-0.8B the effect was small (peak 3.6 → 3.4 GB), because its bf16
weights are small next to the Python around them. Answers from the saved copy
are identical to freshly quantized ones (max difference 0 on 15 pages × 11
questions).

A fresh server also answers faster on a swapped Mac: readings went from 3–20 s
(the old server, mostly swapped out) to 2–3 s right after the restart.

The app starts the server when nothing answers on :8009 (`KEV_URL` points it
elsewhere), and starts it again within ~5 s if it stops or crashes. When
another app holds 8009, the app's server moves to the next free port and
notes it in `models/server.json` while the app runs. Every model call asks
the same server: `status`, `doctor`, `rules test`, `allow test`, `eval` and
the pop-up's explanation look at `KEV_URL` first, then that note, then
:8009, so terminal commands need no `KEV_URL` after a move. With the app not
running and another app on 8009, they refuse ("port 8009 is used by another
app, not a model server, so nothing was sent to it…"); a model you run by
hand on another port needs `KEV_URL`. A listener on 8009 that takes the
connection but doesn't answer in time is asked again, for three times as
long: one that answers like a model server (an `ssh -L` tunnel or a Docker
port forward to a busy model) is used, and only one that stays silent
counts as another app (the model moves; at the app's start this second
look can hold it up to about 12 s). A `KEV_URL` on another machine that's
down or off the network reads "the model server at <host> doesn't answer"
in `qualm status` and doctor alike. `status --json` says why in
`model.why`: `remote`, `starting` (the app's server is downloading or
loading), `taken` ("port N is used by another app, not a model server") or
`none`. A Kev server too busy to answer in time (a Mac deep in swap) counts
as up: `qualm status` says "busy answering" and exits 0, and doctor says
the same.

When starts keep failing, it waits longer each time (5 s, 15 s, 1 minute,
then every 5 minutes), and the menu says why in words: no internet, Hugging
Face busy or blocked, not enough disk, stopped by macOS for memory (details
in `~/Library/Logs/Qualm/kev.log`, which Model > *Show the local model's
log* opens). Quit (in the menu) stops it too. A
server from `qualm serve` is used and left alone. No second model ever
loads: `qualm serve` (and the server itself, before it loads MLX) exits when
the app's server holds the Qualm folder ("already running or getting
ready"), when a model server already answers on the port, or when another
app holds it (it suggests `--port` 8010 and `KEV_URL`); only one runtime
install runs at a time.

### Questions per reading

Kev answers every question in one pass, so up to about 10 rules costs about
the same as 1: 0.4 s for 1 rule, 0.6 s for 10. Past that, one long pass grows
faster than linearly: 1.8 s at 25 rules, 7.9 s at 50, and a timeout (and
14 GB of swap) at 100. Hence the limit of 25 questions a reading
(`[settings] max_questions`). The details are in HANDOFF.md, Measured.
