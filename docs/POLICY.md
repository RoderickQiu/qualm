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
   on purpose (from a search, or a link someone sent) is let through, except
   on the rule's own sites: the starter lists TikTok, Douyin, Kuaishou,
   YouTube Shorts and Reels, so a TikTok someone sent still pops up there
   (*Not this one* lets that one address through). The one after it never
   is. Before the audit, a video someone sent was let through on those
   sites too; a rules.toml made then still says so in its note.
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
- **Search results.** Searching is the intentional act; the page after it is
  where the policy looks. A page the model reads as search results is let
  through for content rules and check-in rules ("search results" in
  `rules test`). A deny rule that judges the page itself (`target = "page"`,
  what `rules add` makes: a shopping rule on a store's results is about the
  store) still judges it, and a rule's own sites still step in.
- **Learning material**: lectures, tutorials, courses, documentation, papers,
  on any site. Each rule can opt out of this exemption; the old stocks rule did,
  because "learning about trading" is how that habit dresses up.
- **Primary sources that happen to sit in a bad category**: filings,
  prospectuses and earnings reports (the stocks rule's exception, when it existed), official
  announcements.
- **Sensitive pages**: logins, payments, bank pages. The model is asked
  (that's how they're found), but no rule acts on them, and the log keeps
  only their site and score: no title, no text, no screenshot. No screenshot
  is kept at all unless the model read the page and found it wasn't private.
  While the model can't answer (a first download, a timeout, a hosted
  outage), nothing is found private, and a rule's own sites and apps still
  step in: that's why the starters list no home page that is a login form
  when you're logged out (Facebook's and Instagram's are left to the model).
- **Password managers**: never read, whatever the settings say.
- **Things arrived at on purpose.** Covered next.

## How you got here matters more than where you are

A single screenshot can't tell a Reddit thread you searched for from one you
drifted into. The watcher can, because it sees the screen before:

- **Opened on purpose**: a single item opened straight from a search-results
  page or a work tool, or a link followed from another app (Slack, your
  editor, a doc, mail). Rules with `allow_intentional` let it through.
- **Drifted into**: an item opened from a feed, or from another item of the
  same kind. That's the rabbit hole, and it gets judged normally.
- **Switching back** to a page that was already open (Cmd-Tab from the
  editor, another tab, Back) opens nothing: the page keeps the status it had
  when it was opened.

The exemption covers that one item. Clicking the next one from there is a
new decision. It never applies on a rule's own sites: a TikTok someone sent
you still pops up (*Not this one* lets that one address through).

## "Take me back"

While a pop-up is open the dimmed screen takes the clicks (`block_clicks`,
on by default; the menu bar switches it): browsing on underneath meant
the answer landed on a different page, or never came. The keyboard still
reaches other apps, and nothing else is locked.

One step back is rarely out: from a 小红书 note it lands on the explore
feed, from there on a profile. So Take me back (and Done) goes back until
the tab is off the pop-up's site, or on a page of it Qualm judged fine
(the lecture you came from before the Shorts), up to 12 steps; with nothing
to go back to, a new-tab page. How it gets there depends on the browser:

- Chromium browsers (Chrome and its Beta, Dev and Canary, Chromium, Brave,
  Edge, Vivaldi, Opera, Arc, Dia, Comet, ChatGPT Atlas, Yandex, Whale, QQ
  Browser, ego lite) go back through their AppleScript dictionary; macOS
  asks once to let Qualm control each (Automation). One that refuses gets
  the keys instead.
- Safari goes back with its Back shortcut, pressed through System Events,
  and Qualm reads the address back through Safari's AppleScript to see where
  it landed: macOS asks once to let Qualm control Safari, and once for
  System Events.
- Firefox, Zen, Orion, DuckDuckGo, SigmaOS, Safari Technology Preview and
  other Gecko or WebKit browsers get Cmd-[ with the address read back
  through Accessibility (or the title), then Cmd-T for a new tab if nothing
  changed. The keys go through System Events, which macOS also asks about
  once.
- Wherever it presses keys (Safari's Back, these browsers, a Chromium
  browser without Automation), Qualm brings the browser forward once and
  presses a key only while it's the app in front. If you switch to another
  app before it's done, it stops there, and doesn't pop up again on that
  page until the page changes.
- A browser missing from Qualm's table is known by its window: a page with
  an http(s) address and, outside the page, a tab strip or an address field
  showing its site. It gets one Cmd-[ while it's in front, with the address
  read back, and never Cmd-T (in an app Qualm doesn't know, that key could
  be anything); closing its window would lose every tab in it.
- Any other app, even one showing a web page (a web app installed from
  Chrome or Edge, Slack and other Electron apps whose tabs, if any, are
  inside the page, an app a rule names), gets what every app gets: that
  window closes if the app has others open, else the app is hidden.

A browser window is never closed. If you already left the site while the
pop-up was up, it does nothing, and a Back that couldn't leave doesn't
count as coming back (the wait before *I need it* doesn't grow).

## When the user says "I need this"

Every intervention offers two ways out besides going back:

- **"I need it: 10 min"** snoozes the rule and asks why. The reason is
  logged. Check-ins and focus sessions are the fuller version of this.
- **"Not this one"** says the model was wrong. The page's address is allowed
  for that rule from now on (that exact address: the same page with another
  query string is another one); in an app window with no address, that
  window is let through below the score it had, plus 0.1, or at any score
  on a rule's own app. On a rule's own sites and apps it, and *Never here*,
  wait as long as *I need it* does. It doesn't change the
  question the model reads: page titles added to it as words raised other
  pages' scores (two of them took YouTube's home page from 0.20 to 0.34 on
  social, over its threshold). Exceptions you type (`qualm except add`, the
  dashboard) are the words the model reads. This is the desktop version of
  SeeNot's repair rules (`FalsePositiveRuleGenerator.kt`).
- **"It's part of the task"**, in a focus session, lets that page through
  until the session the pop-up was shown in ends, and saves nothing.
  Clicked after that session ended, it lets nothing through and says so
  ("Your focus session had already ended, so nothing was changed."); a
  *Not this one* shown before a session started is saved for good.

## Generalizing to sites nobody listed

- Rules are written as **descriptions of a mode** ("algorithmic feeds,
  timelines and trending lists"), and the model judges them from the page
  text. Site names in a rule should be examples ("such as …"), not a scope:
  worded "entertainment videos on Bilibili or YouTube", the video rule let
  Netflix, Prime Video and Vimeo through (0.12-0.15 on Kev-4B, where the
  same wording without the sites scores 0.50-0.67), so the starter names none.
  The social and livestream starters' examples lead with sites people use
  everywhere (Instagram, Facebook, Twitch, Kick), then the Chinese ones the
  trials were made on. Short video and feeds kept their wording (Douyin and
  Kuaishou lead the short-video examples, Bilibili and Weibo follow YouTube
  in the feeds ones): the variants tried moved nothing, or lifted
  LinkedIn's feed to 0.77 on feeds, a false pop-up in waiting.
- Every reading also asks three site-independent questions: is it sensitive,
  what kind of page is it (feed, single item, search, work), and what is it for
  (learn, task, entertain). The exemptions above run on these, so they
  apply to a site the rules never mention.
- URL `patterns` are a shortcut for sites the model is known to miss (Twitch
  channels have no "live" text in the Accessibility tree). They're never the
  only way a rule fires. They cut both ways: the Twitch pattern also catches
  a live coding stream, which the model reads as entertainment anyway (0.96).
- `apps` are the same for apps that show little but their name (a game, a
  TV app): a hit for that rule whenever the app is in front, without asking
  the model about it; the other rules are still judged there. Naming a
  browser makes every page in it a hit for that rule.
- Allow classes let a kind of page through (a store, a music player, a
  chat), and their listed sites and apps without asking the model. A rule
  that lists the class in `overrides_allow`, or names that very app or
  site (Spotify, open.spotify.com, music.youtube.com) or a narrower one,
  steps in there too: Qualm asks the model on those places instead of
  letting them through unasked, and every other rule still lets them
  through. A rule on a whole domain doesn't count as naming its
  subdomains here: one on youtube.com, qq.com or 163.com leaves YouTube
  Music, QQ Music and NetEase Cloud Music to the music class.
- Thresholds are checked with sites held out (`experiments/analyze.py
  --by-site`): a threshold picked on some sites has to hold on the others.

## Personalizing

Everything that differs between people lives in `rules.toml`, in your own
words, and every part of it is a command (docs/PERSONALIZE.md), so an
agent can set it up as well as you can:

- which modes you care about, and in what words (`rules add`, `rules set ID what=...`);
- per-rule `threshold` and `kind` (`deny` or `check_in`); `max_wait_s` and `extensions`;
- per-rule `allow_learning` and `allow_intentional`;
- when a rule applies (`when = ["mon-fri 09:00-12:00, 13:00-18:00"]`), and whether it's on at all;
- the sites and apps a rule always covers (`sites` as plain domains, `apps` by name);
- the allow classes a rule steps in on anyway (`overrides_allow`: a
  shopping rule and the `shopping` class);
- `exceptions` you type, and the pages "Not this one" let through;
- `[settings] no_monitor` (apps never read) and `allow_sites` (never judged).

A new rule is tested before it's trusted: `rules test` asks the model that
rule on your own recent screens and shows what it would do on each, and
`rules tune` picks its threshold from your yes/no answers. On a first day
there's nothing to test on: the rule starts with its first-guess threshold,
and is tested after a day of use.

It also learns from you over time:

1. Each decision and your response is logged to `data/decisions.jsonl`.
2. "Not this one" and "Take me back" are labels, so the log turns into
   `labels.jsonl` records without a separate labeling session
   (`qualm harvest`).
3. `qualm rules tune` (or Apply on the dashboard's Rules tab) re-picks a
   rule's threshold from your answers, and never picks one that does worse
   on them than the current one.
4. With a few hundred of your own labels, fine-tune Kev on them
   (`qualm export`; see HANDOFF.md).

Who you are changes the defaults a lot. A trader would never add a stocks rule, a video
creator needs `shortvideo` off during work hours (`rules set shortvideo
'when=["sat-sun", "mon-fri 18:00-24:00"]'`), and a student may want `videos`
to step in at once during exam weeks (`rules set videos kind=deny`). The rules file is where that goes.
There's deliberately no built-in site list to fight against.
