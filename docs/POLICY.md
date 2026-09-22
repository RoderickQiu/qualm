# What to block on a desktop, and what not to

This is the reasoning behind `rules.example.toml` and `policy.py`. The short
version: **block the mechanism, not the site.**

## Why the site is the wrong unit

On a phone, a distracting app is mostly one thing: opening Douyin means
scrolling Douyin. On a desktop, the same site is both a tool and a trap:

| Site | Can be good | Is usually the trap |
|---|---|---|
| YouTube | a lecture, a conference talk, a repair tutorial you searched for | the home page, Shorts, the autoplay sidebar after the video you came for |
| Bilibili | 宋浩高数, a technical talk, a course playlist | the home recommendations, 鬼畜 and 盘点 clips, live rooms |
| Reddit | the thread Google found for your error message | r/popular, then the next thread, then the next |
| X / Weibo | an announcement or thread someone linked you to; emergency news | For You, 热搜, the profile you check out of habit |
| Xiaohongshu / Zhihu | a travel guide or one answer you searched for | the explore page, the hot list |
| Stock sites | a 10-K, an earnings report, checking your own position once for a decision | refreshing quotes, candlestick charts and 股吧 all day |
| Douyin / TikTok | one video a friend sent | the next video, which you didn't choose |
| Twitch / live rooms | a live-streamed lecture or launch event | an entertainment stream left running |

Blocking by domain gets both columns wrong at once: it breaks the lecture and
the tutorial, and people learn to bypass it. So the question is not "which
site" but "which mode".

## The harmful modes

These are the things that should be stopped, whatever site they appear on:

1. **Infinite short-form video.** The format is the harm: each item is chosen
   for you and the next one is always one swipe away. No learning exemption;
   a "tutorial Short" is still a slot machine pull. A single video opened
   from a link someone sent is fine; the one after it is not.
2. **Algorithmic feeds, timelines and trending lists.** Home pages, For You,
   热搜, explore, r/popular. You didn't pick anything yet; the page picks for
   you. This is judged on the page itself, unlike content rules.
3. **Entertainment livestreams.** There's no end and no natural stopping point.
   A live lecture or launch event is exempt.
4. **Compulsive checking.** Stock quotes, a social profile, an inbox. One
   check can be a decision; the twelfth is a habit. This is a visit limit, not
   a ban.
5. **Unbounded entertainment video and social browsing.** Not bad in itself.
   These get a daily time budget, not a block.

## What should never be blocked

- **Work tools**: editors, terminals, documents, design tools, spreadsheets,
  even when the words on screen match a rule (a quant's spreadsheet full of
  tickers, a creator editing their own short video).
- **Search results.** Searching is the intentional act; the page after it
  is where the policy looks.
- **Learning material**: lectures, tutorials, courses, documentation, papers,
  on any site. Each rule can opt out of this exemption; `stocks` does,
  because "learning about trading" is how that habit dresses up.
- **Primary sources that happen to sit in a bad category**: filings,
  prospectuses and earnings reports (the stocks rule's exception), official
  announcements.
- **Sensitive pages**: logins, payments, bank pages. They are not judged at
  all, and not logged.
- **Things arrived at on purpose.** Covered next.

## How you got here matters more than where you are

A single screenshot can't tell a Reddit thread you searched for from one you
drifted into. The watcher can, because it sees the screen before:

- **Opened on purpose**: a single item opened within two minutes of a
  search-results page or a work app (a link from Slack, from your editor,
  from a doc). Rules with `allow_intentional` let it through.
- **Drifted into**: an item opened from a feed, or from another item of the
  same kind. That's the rabbit hole, and it gets judged normally.

The exemption covers that one item. Clicking the next one from there is a
new decision.

## When the user says "I need this"

Every intervention offers two ways out besides going back:

- **"I need it: 10 min"** snoozes the rule and asks why. The reason is
  logged. SeeNot's session intents are the fuller version of this.
- **"Not this one"** says the model was wrong. The page's URL is allowed for
  that rule from now on, and its title becomes one of the rule's
  `exceptions`, which is the text the model reads. This is the desktop
  version of SeeNot's repair rules (`FalsePositiveRuleGenerator.kt`).

## Generalizing to sites nobody listed

- Rules are written as **descriptions of a mode** ("algorithmic feeds,
  timelines and trending lists"), and the model judges them from the page
  text. The site names in a rule are examples, not the list.
- Every reading also asks three site-independent questions: is it sensitive,
  what kind of page is it (feed, single item, search, work), and what is it for
  (learn, task, entertain). The exemptions above run on these, so they
  apply to a site the rules never mention.
- URL `patterns` are a shortcut for sites the model is known to miss (Twitch
  channels have no "live" text in the Accessibility tree). They're never the
  only way a rule fires.
- Thresholds are checked with sites held out (`experiments/analyze.py
  --by-site`): a threshold picked on some sites has to hold on the others.

## Personalizing

Everything that differs between people lives in `rules.toml`, in your own
words and language:

- which modes you care about, and in what words;
- per-rule `threshold`, `minutes_per_day`, `visits_per_day`;
- per-rule `allow_learning` and `allow_intentional`;
- `exceptions` (typed, or added by "Not this one");
- `[settings] no_monitor` (apps never read) and `allow_urls` (never judged).

It also learns from you over time:

1. Each decision and your response is logged to `data/decisions.jsonl`.
2. "Not this one" and "Take me back" are labels, so the log turns into
   `labels.jsonl` records without a separate labeling session
   (`seenot-desktop harvest`).
3. `seenot-desktop eval --suggest` re-picks each rule's threshold from your
   own labels.
4. With a few hundred of your own labels, fine-tune Kev on them
   (`seenot-desktop export`; see HANDOFF.md).

Who you are changes the defaults a lot. A trader needs `stocks` off, a video
creator needs `shortvideo` off during work hours, and a student may want
`videos` at 0 minutes during exam weeks. The rules file is where that goes.
There's deliberately no built-in site list to fight against.
