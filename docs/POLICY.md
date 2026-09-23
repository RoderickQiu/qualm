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
4. **Compulsive checking.** A social profile, an inbox, stock quotes. One
   check can be a decision; the twelfth is a habit. A check-in rule (below)
   makes each one a little slower than the last, never a ban. The shipped
   rules don't include stocks any more: it's a niche habit, and in real use
   it fired on shopping chats and on Qualm's own review page. Add it back if
   it's yours.
5. **Unbounded entertainment video and social browsing.** Not bad in itself;
   the harm is the two minutes that become forty. These rules check in
   (`kind = "check_in"`), not block.

## Check-ins, not daily budgets

A daily budget ("45 minutes of video") was the first design, and it's the
wrong one. It reads as an allowance to use up; the first 44 minutes, the
morning's first idle check included, get no friction at all; the cutoff
lands at a random moment; and once it's blown, people give up on it or
turn the tool off. It also can't see what hurts: the short check that turns
into forty minutes, or the fifteenth check in an hour.

So a check-in rule asks at the door and holds you to your own answer:

1. **On arrival**: "What are you here for?" (Not for learning material with
   `allow_learning`, nor a link or search result you opened on purpose with
   `allow_intentional`.) A few words,
   and 5, 15 or 30 minutes. 5 is preselected, so Return means 5.
2. **During the session** nothing pops up. The session covers the rule,
   not the site: YouTube then Bilibili is one session; and a page two
   check-in rules both hit (a video on a social site) asks once.
3. **When the time is up and you're still there**: "Your 15 minutes for
   'the match' are up." Done is the default and takes you back. "5 more"
   is there once per session (`extensions`), after a 10 s wait; after that,
   only a new check-in. Still on the page 30 s after going back (an app with
   no Back): it steps in again.
4. **Each session costs a little more.** The wait before Start unlocks: the
   first session of the day is free, then 5, 10, 20, 40 s, up to 60
   (`max_wait_s`); doubled again when you come back within 20 minutes of a
   session ending; 15 minutes adds 5 s and 30 minutes 10 s.

Never a block: the most it costs is a minute's wait. The pop-up states
where you are ("3rd time today · 52 min so far · last ended 14:20"), and
the dashboard shows each session: what for, how long you said, how long you
stayed. This follows one sec (PNAS 2023): a short pause and a question at
each opening cut use, where the message alone didn't.

## What should never be blocked

- **Work tools**: editors, terminals, documents, design tools, spreadsheets,
  even when the words on screen match a rule (a quant's spreadsheet full of
  tickers, a creator editing their own short video).
- **Search results.** Searching is the intentional act; the page after it
  is where the policy looks.
- **Learning material**: lectures, tutorials, courses, documentation, papers,
  on any site. Each rule can opt out of this exemption; the old stocks rule did,
  because "learning about trading" is how that habit dresses up.
- **Primary sources that happen to sit in a bad category**: filings,
  prospectuses and earnings reports (the stocks rule's exception, when it existed), official
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

## "Take me back"

While a pop-up is open the dimmed screen takes the clicks (`block_clicks`,
on by default; the menu bar switches it): browsing on underneath meant
the answer landed on a different page, or never came. The keyboard still
reaches other apps, and nothing else is locked.

One step back is rarely out: from a 小红书 note it lands on the explore
feed, from there on a profile. So Take me back (and Done) goes back until
the tab is off the pop-up's site, or on a page of it Qualm judged fine
(the lecture you came from before the Shorts), up to 12 steps; with nothing
to go back to, a new-tab page. Chrome, Brave and Edge are driven through
their AppleScript dictionary (macOS asks once to allow it), Safari with its
Back shortcut; other browsers get one Back. If you already left the site
while the pop-up was up, it does nothing.

## When the user says "I need this"

Every intervention offers two ways out besides going back:

- **"I need it: 10 min"** snoozes the rule and asks why. The reason is
  logged. Check-ins and focus sessions are the fuller version of this.
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
words, and every part of it is a command (docs/PERSONALIZE.md), so an
agent can set it up as well as you can:

- which modes you care about, and in what words (`rules add`, `rules set ID what=...`);
- per-rule `threshold` and `kind` (`deny` or `check_in`); `max_wait_s` and `extensions`;
- per-rule `allow_learning` and `allow_intentional`;
- when a rule applies (`when = ["mon-fri 09:00-18:00"]`), and whether it's on at all;
- the sites a rule always covers, as plain domains (`sites`);
- `exceptions` (typed, or added by "Not this one");
- `[settings] no_monitor` (apps never read) and `allow_sites` (never judged).

A new rule is tested before it's trusted: `rules test` asks the model that
rule on your own recent screens and shows what it would do on each, and
`rules tune` picks its threshold from your yes/no answers.

It also learns from you over time:

1. Each decision and your response is logged to `data/decisions.jsonl`.
2. "Not this one" and "Take me back" are labels, so the log turns into
   `labels.jsonl` records without a separate labeling session
   (`qualm harvest`).
3. `qualm eval --suggest` re-picks each rule's threshold from your
   own labels.
4. With a few hundred of your own labels, fine-tune Kev on them
   (`qualm export`; see HANDOFF.md).

Who you are changes the defaults a lot. A trader would never add a stocks rule, a video
creator needs `shortvideo` off during work hours (`rules set shortvideo
'when=["sat-sun", "mon-fri 18:00-24:00"]'`), and a student may want `videos`
to step in at once during exam weeks (`rules set videos kind=deny`). The rules file is where that goes.
There's deliberately no built-in site list to fight against.
