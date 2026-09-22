"""Pages for the automated labeling trial, with labels decided by what the
page is. Rules match rules.example.toml: shortvideo (deny), stocks (deny),
social (time_cap). Labels are the collector's judgement, not the user's."""


def L(target, page, sv="safe", st="safe", so="out_of_scope", sens=False, note=""):
    return {"target": target, "labels": {"sensitive": sens, "page_kind": page,
            "rules": {"shortvideo": sv, "stocks": st, "social": so}}, "note": note}


V, S = "violates", "safe"
YT_SHORTS = ["r-p0gHQsxwg", "d99vrWc2m7E", "hpRIodZG6Dk", "gnO78rr-38s", "-k0w2nViTVk", "ipxkqTZj6P8",
             "f_4rrQQOe_Q", "qPa7aH-QcTg", "-mzTKMSu3GI", "QsScnZ82oVI"]
BILI_FUN = ["BV1mQhi6wE8u", "BV1XjhH6fEk4", "BV14Nhn6EEgs", "BV1PShE66EnA", "BV14Ahn6SE2K", "BV1ncYs6sEe3"]
BILI_LIVE = ["545068", "1967216004", "7734200", "9048914"]
TWITCH = ["zackrawrr", "hasanabi", "ironmouse", "jynxzi"]
BILI_COURSE = ["BV1Eb411u7Fw", "BV1vJ41187db", "BV1aP4y1o7n1"]

PAGES = (
    # short video / livestream: violations
    [L(f"https://www.youtube.com/shorts/{i}", "single_item", sv=V) for i in YT_SHORTS]
    + [L(f"https://www.bilibili.com/video/{i}/", "single_item", sv=V) for i in BILI_FUN]
    + [L(f"https://live.bilibili.com/{i}", "single_item", sv=V) for i in BILI_LIVE]
    + [L(f"https://www.twitch.tv/{i}", "single_item", sv=V) for i in TWITCH]
    + [L("https://www.douyin.com/", "single_item", sv=V, note="douyin home autoplays one video"),
       L("https://www.kuaishou.com/new-reco", "single_item", sv=V),
       L("https://www.tiktok.com/foryou", "single_item", sv=V)]
    # video, but not short video
    + [L(f"https://www.bilibili.com/video/{i}/", "single_item", note="long course video") for i in BILI_COURSE]
    + [L("https://www.youtube.com/", "feed"), L("https://www.bilibili.com/", "feed"),
       L("https://www.twitch.tv/directory/all", "feed"), L("https://live.bilibili.com/", "feed"),
       L("https://www.youtube.com/results?search_query=rust+tutorial", "search")]
    # stocks: violations
    + [L(f"https://finance.yahoo.com/quote/{t}/", "single_item", st=V) for t in ("AAPL", "TSLA", "NVDA", "BABA")]
    + [L(f"https://www.google.com/finance/quote/{t}", "single_item", st=V) for t in ("AAPL:NASDAQ", "TSLA:NASDAQ", "600519:SHA")]
    + [L(f"https://xueqiu.com/S/{t}", "single_item", st=V) for t in ("SH600519", "SZ000001", "TSLA")]
    + [L(f"https://quote.eastmoney.com/{t}.html", "single_item", st=V) for t in ("sh600519", "sz300750")]
    + [L(f"https://guba.eastmoney.com/list,{t}.html", "feed", st=V, note="stock forum list") for t in ("600519", "300750")]
    + [L(f"https://stocktwits.com/symbol/{t}", "single_item", st=V) for t in ("AAPL", "TSLA")]
    + [L("https://www.tradingview.com/symbols/NASDAQ-NVDA/", "single_item", st=V),
       L("https://www.tradingview.com/chart/?symbol=NASDAQ%3AAAPL", "single_item", st=V),
       L("https://finance.sina.com.cn/realstock/company/sh600519/nc.shtml", "single_item", st=V),
       L("https://www.cnbc.com/quotes/AAPL", "single_item", st=V),
       L("https://www.marketwatch.com/investing/stock/aapl", "single_item", st=V),
       L("https://www.investing.com/equities/apple-computer-inc", "single_item", st=V),
       L("https://www.reddit.com/r/wallstreetbets/", "feed", st=V, so="in_scope"),
       L("https://www.reddit.com/r/wallstreetbets/comments/1wn58ds/daily_discussion_thread_for_september_22_2026/", "single_item", st=V, so="in_scope"),
       L("https://www.reddit.com/r/wallstreetbets/comments/1wn7e2x/with_mr_buffett_retiring_i_would_like_to_make/", "single_item", st=V, so="in_scope")]
    # finance, but not stocks: the exception (filings) and neutral finance
    + [L("https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm", "single_item", note="10-K filing: exception"),
       L("https://investor.apple.com/sec-filings/default.aspx", "other", note="filings index: exception"),
       L("https://www.federalreserve.gov/monetarypolicy/openmarket.htm", "single_item"),
       L("https://www.xe.com/currencyconverter/", "other")]
    # social: in scope
    + [L("https://x.com/elonmusk", "feed", so="in_scope"), L("https://x.com/explore", "other", so="in_scope", sens=True, note="login wall"),
       L("https://x.com/NASA", "feed", so="in_scope"),
       L("https://weibo.com/", "feed", so="in_scope"), L("https://s.weibo.com/top/summary", "feed", so="in_scope"),
       L("https://m.weibo.cn/", "feed", so="in_scope"),
       L("https://www.xiaohongshu.com/explore", "feed", so="in_scope"),
       L("https://web.okjike.com/", "other", so="in_scope", sens=True, note="login wall"),
       L("https://www.reddit.com/r/popular/", "feed", so="in_scope"),
       L("https://www.reddit.com/r/programming/", "feed", so="in_scope"),
       L("https://www.reddit.com/r/programming/comments/1wmqwd3/til_0xcafebabe/", "single_item", so="in_scope"),
       L("https://www.reddit.com/r/programming/comments/1w06vn1/how_we_saved_100_terabytes_of_memory_by/", "single_item", so="in_scope"),
       L("https://www.threads.com/", "feed", so="in_scope"),
       L("https://bsky.app/", "feed", so="in_scope"),
       L("https://www.instagram.com/natgeo/", "feed", so="in_scope"),
       L("https://www.facebook.com/", "other", so="in_scope", sens=True, note="login wall"),
       L("https://x.com/i/flow/login", "other", so="in_scope", sens=True, note="login page of a social site")]
    # sensitive: login and payment
    + [L("https://accounts.google.com/signin", "other", sens=True),
       L("https://github.com/login", "other", sens=True),
       L("https://account.apple.com/sign-in", "other", sens=True),
       L("https://www.paypal.com/signin", "other", sens=True),
       L("https://login.taobao.com/", "other", sens=True),
       L("https://passport.bilibili.com/login", "other", sens=True),
       L("https://www.amazon.com/ap/signin?openid.pape.max_auth_age=0&openid.return_to=https%3A%2F%2Fwww.amazon.com%2F&openid.identity=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select&openid.assoc_handle=usflex&openid.mode=checkid_setup&openid.claimed_id=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select&openid.ns=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0", "other", sens=True),
       L("https://secure.chase.com/web/auth/dashboard", "other", sens=True),
       L("https://www.bankofamerica.com/", "other", sens=True, note="bank home with sign-in box"),
       L("https://auth.alipay.com/login/index.htm", "other", sens=True),
       L("https://signup.live.com/", "other", sens=True)]
    # shopping, not sensitive
    + [L("https://www.amazon.com/dp/B0BSHF7WHW", "single_item"), L("https://www.apple.com/shop/buy-iphone", "single_item")]
    # work and reading
    + [L("https://github.com/jaredpalmer/kev", "work"),
       L("https://github.com/jaredpalmer/kev/blob/main/kev/serve.py", "work"),
       L("https://docs.python.org/3/library/asyncio.html", "single_item"),
       L("https://developer.apple.com/documentation/applicationservices/axuielement_h", "single_item"),
       L("https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Promise", "single_item"),
       L("https://en.wikipedia.org/wiki/Attention_(machine_learning)", "single_item"),
       L("https://zh.wikipedia.org/wiki/%E6%B7%B1%E5%BA%A6%E5%AD%A6%E4%B9%A0", "single_item"),
       L("https://arxiv.org/abs/1706.03762", "single_item"),
       L("https://stackoverflow.com/questions/231767/what-does-the-yield-keyword-do-in-python", "single_item"),
       L("https://news.ycombinator.com/", "feed", note="HN: link aggregator, not in the social list"),
       L("https://www.bbc.com/news", "feed"), L("https://36kr.com/", "feed"), L("https://sspai.com/", "feed"),
       L("https://www.zhihu.com/question/19550225", "single_item", so="unknown", note="zhihu: social or not is unclear"),
       L("https://www.google.com/search?q=macos+accessibility+api", "search"),
       L("https://www.baidu.com/s?wd=%E6%9C%BA%E5%99%A8%E5%AD%A6%E4%B9%A0", "search"),
       L("https://www.bing.com/search?q=typesafe+jev", "search"),
       L("https://www.notion.so/", "other"), L("https://www.figma.com/", "other"),
       L("https://mail.google.com/", "other", sens=True, note="gmail sign-in when logged out"),
       L("https://translate.google.com/", "work"), L("https://www.overleaf.com/", "other"),
       L("https://www.google.com/maps", "other"), L("https://weather.com/", "other")]
    # native apps, captured in the background
    + [L("app:com.apple.Terminal", "work"), L("app:com.todesktop.230313mzl4w4u92", "work"),
       L("app:com.apple.finder", "other"), L("app:com.apple.systempreferences", "other"),
       L("app:com.apple.Notes", "work"), L("app:com.apple.iWork.Numbers", "work")]
)
