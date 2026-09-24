/* Qualm site: the pinned story, the judge, and the pop-up at the end. */
(function () {
  'use strict';
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ----------------------------------------------------------- the story */
  const story = $('#story'), mac = $('#mac'), roll = $('#roll'), count = $('#count');
  const pages = { lecture: $('#pg-lecture'), feed: $('#pg-feed'), thread: $('#pg-thread'), popular: $('#pg-popular'), film: $('#pg-film') };
  const panels = { short: $('#panel'), feed: $('#panel-feed'), ci: $('#panel-ci'), up: $('#panel-up') };
  const ORD = ['1st', '2nd', '3rd', '4th', '5th', '6th'];
  const STEPS = 9;
  let step = -1, swipeTimer = null, swipes = 0, countdown = null;

  const SCENES = [
    // [page, title, url, dimmed, panel]
    ['lecture', 'Lecture 3: Linear Regression - YouTube', 'youtube.com/watch?v=4b4MUYve_U8', false, null],
    ['lecture', 'Lecture 3: Linear Regression - YouTube', 'youtube.com/watch?v=4b4MUYve_U8', false, null],
    ['feed', 'Try Not To Smile Challenge #shorts - YouTube', 'youtube.com/shorts/x7Qk2mRw', false, null],
    ['feed', 'this cat has had enough #shorts - YouTube', 'youtube.com/shorts/9aFk3pLm', false, null],
    ['feed', 'this cat has had enough #shorts - YouTube', 'youtube.com/shorts/9aFk3pLm', true, 'short'],
    ['lecture', 'Lecture 3: Linear Regression - YouTube', 'youtube.com/watch?v=4b4MUYve_U8', false, null],
    ['thread', "TypeError: cannot read properties of undefined : r/reactjs", 'reddit.com/r/reactjs/comments/1f3k9x', false, null],
    ['popular', 'Popular posts - Reddit', 'reddit.com/r/popular', true, 'feed'],
    ['film', 'The Grand Budapest Hotel - Netflix', 'netflix.com/watch/70295915', true, 'ci'],
  ];

  function swipeTo(n) {
    swipes = n;
    roll.style.transform = 'translateY(' + (-(n % 5) * 100) + '%)';
    count.innerHTML = '<b>' + ORD[Math.min(n, 5)] + '</b> in a row';
  }
  function startSwiping(fast) {
    clearInterval(swipeTimer);
    if (reduce) return;
    swipeTimer = setInterval(() => swipeTo(swipes + 1), fast ? 1100 : 1900);
  }
  function setWait(n) {
    const b = $('#p-wait');
    if (n > 0) { b.textContent = 'Wait ' + n + ' s'; b.disabled = true; b.classList.remove('ready'); }
    else { b.textContent = 'I need it'; b.disabled = false; b.classList.add('ready'); }
  }
  function armWait() {
    let n = 3; setWait(n); clearInterval(countdown);
    countdown = setInterval(() => { n -= 1; setWait(n); if (n <= 0) clearInterval(countdown); }, 1000);
  }

  function go(n) {
    if (n === step) return;
    const prev = step; step = n;
    story.dataset.step = n;
    $$('.cap').forEach((c) => c.classList.toggle('on', +c.dataset.cap === n));
    $$('#dots li').forEach((d, i) => d.classList.toggle('on', i === n));
    const [page, title, url, dim, panel] = SCENES[n];
    $('#mac-title').textContent = title; $('#mac-url').textContent = url;
    Object.entries(pages).forEach(([k, el]) => el.classList.toggle('on', k === page));
    Object.entries(panels).forEach(([k, el]) => el.classList.toggle('on', k === panel));
    mac.classList.toggle('dimmed', dim);
    clearInterval(swipeTimer); clearInterval(countdown);
    if (page === 'feed') {
      if (n === 2) { swipeTo(prev < 2 ? 0 : 1); startSwiping(false); }
      if (n === 3) { swipeTo(3); startSwiping(true); }
      if (n === 4) { swipeTo(3); armWait(); }
    }
    if (n === 7) armFeedWait();
    if (n === 8) { panels.ci.classList.add('on'); panels.up.classList.remove('on'); $('#ci-what').value = ''; }
  }
  function armFeedWait() {
    const b = $('#panel-feed .p-wait'); let n = 3;
    b.textContent = 'Wait 3 s'; b.disabled = true; b.classList.remove('ready');
    countdown = setInterval(() => { n -= 1; if (n > 0) b.textContent = 'Wait ' + n + ' s'; else { b.textContent = 'I need it'; b.disabled = false; b.classList.add('ready'); clearInterval(countdown); } }, 1000);
  }

  // progress dots
  const dots = $('#dots');
  for (let i = 0; i < STEPS; i++) dots.appendChild(document.createElement('li'));

  // which step is on screen: the step whose block crosses the middle of the viewport
  const steps = $$('.steps .step');
  function onScroll() {
    if (document.body.classList.contains('preview')) return;
    const mid = innerHeight * 0.5;
    let n = 0;
    steps.forEach((s, i) => { if (s.getBoundingClientRect().top <= mid) n = i; });
    go(n);
  }
  let ticking = false;
  addEventListener('scroll', () => { if (!ticking) { ticking = true; requestAnimationFrame(() => { onScroll(); ticking = false; }); } }, { passive: true });
  addEventListener('resize', onScroll);

  // ?scene=N (or judge, end) shows one scene without scrolling, to check the page in a headless browser
  const q = new URLSearchParams(location.search).get('scene');
  if (q !== null) {
    document.body.classList.add('preview');
    if (q === 'judge' || q === 'end' || q === 'lines') {
      story.hidden = true; $('.lines').hidden = q !== 'lines'; $('.judge').hidden = q !== 'judge'; $('#end').hidden = q !== 'end';
      if (q === 'end') $('#end').classList.add('on'); if (q === 'judge') document.body.classList.add('on-paper');
    } else { story.classList.add('preview'); $('.judge').hidden = true; $('.lines').hidden = true; $('#end').hidden = true; }
    go(Math.max(0, Math.min(STEPS - 1, +q || 0)));
  } else onScroll();

  // the pop-up's own buttons, at step 4
  panels.short.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-act]'); if (!b) return;
    const act = b.dataset.act;
    if (act === 'back') { go(5); scrollTo({ top: steps[5].offsetTop + 1, behavior: reduce ? 'auto' : 'smooth' }); return; }
    mac.classList.remove('dimmed'); panels.short.classList.remove('on');
    const cap = $('.cap[data-cap="4"]');
    cap.textContent = act === 'need' ? 'Unlocked, and it asks what for. The next wait is longer.'
      : act === 'notthis' ? 'This one address is let through. The next Short is not.'
      : 'youtube.com is silenced for every rule. Undo it on the dashboard.';
    startSwiping(true);
  });

  // check-in, at step 8: a minute lasts a second
  let ciMin = 5, ciTimer = null;
  $('#ci-min').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-min]'); if (!b) return;
    ciMin = +b.dataset.min;
    $$('#ci-min button').forEach((x) => x.setAttribute('aria-checked', x === b ? 'true' : 'false'));
    $('#ci-start').textContent = 'Start ' + ciMin + ' min';
  });
  $('#ci-what').addEventListener('keydown', (e) => { if (e.key === 'Enter') startSession(ciMin); });
  $('#ci-start').addEventListener('click', () => startSession(ciMin));
  $('#ci-more').addEventListener('click', () => startSession(5, true));
  $('#ci-done').addEventListener('click', () => { panels.up.classList.remove('on'); mac.classList.remove('dimmed'); pages.film.classList.remove('on'); pages.lecture.classList.add('on'); $('#mac-title').textContent = 'Lecture 3: Linear Regression - YouTube'; $('#mac-url').textContent = 'youtube.com/watch?v=4b4MUYve_U8'; });
  function startSession(mins, extra) {
    const what = $('#ci-what').value.trim() || 'the film';
    panels.ci.classList.remove('on'); panels.up.classList.remove('on'); mac.classList.remove('dimmed');
    const cap = $('.cap[data-cap="8"]');
    cap.textContent = 'Quiet for ' + mins + ' minutes. Here, a minute lasts a second.';
    clearTimeout(ciTimer);
    ciTimer = setTimeout(() => {
      if (step !== 8) return;
      $('#up-h').textContent = 'Your ' + (extra ? '5 more minutes' : mins + ' minutes') + ' for “' + what + '” are up.';
      $('#ci-more').disabled = !!extra; $('#ci-more').classList.toggle('ready', !extra);
      mac.classList.add('dimmed'); panels.up.classList.add('on');
      cap.textContent = 'Time’s up, in your own words. Done takes you back.';
    }, reduce ? 300 : mins * 1000);
  }

  // dark bar on dark sections, ink on paper
  const paperSections = $$('.judge, .end');
  const io = new IntersectionObserver((es) => {
    es.forEach((e) => { if (e.isIntersecting) document.body.classList.toggle('on-paper', !e.target.classList.contains('end') || !e.target.classList.contains('on')); });
  }, { rootMargin: '-40px 0px -85% 0px' });
  paperSections.forEach((s) => io.observe(s));
  const dark = new IntersectionObserver((es) => { es.forEach((e) => { if (e.isIntersecting) document.body.classList.remove('on-paper'); }); }, { rootMargin: '-40px 0px -85% 0px' });
  $$('.story, .lines').forEach((s) => dark.observe(s));

  /* ----------------------------------------------------------- judge */
  const RULES = [
    ['private', 0.5], ['page kind', null], ['for', null],
    ['shortvideo', 0.15], ['feeds', 0.5], ['livestream', 0.5], ['videos', 0.25], ['social', 0.3],
  ];
  const PAGES = [
    { site: 'YouTube', c: '#ff0000', label: 'a lecture', title: 'Lecture 3: Linear Regression - YouTube', url: 'youtube.com/watch?v=4b4MUYve_U8', from: 'google.com/search?q=gradient+descent+lecture',
      screen: '<div class="vid play"></div><p class="h">Lecture 3: Linear Regression and Gradient Descent</p><p class="m">Stanford Online · 2.4M views</p><div class="l w90"></div><div class="l w60"></div>',
      ans: ['no 0.02', 'single item 0.90', 'learning 0.93', 'safe 0.03', 'safe 0.02', 'safe 0.01', 'out of scope 0.09', 'out of scope 0.02'],
      verdict: ['quiet', 'Left alone: learning material.'],
      decide: 'Every rule is under its threshold, and the videos rule exempts learning anyway.' },
    { site: 'YouTube', c: '#ff0000', label: 'Shorts', title: 'Try Not To Smile Challenge #shorts - YouTube', url: 'youtube.com/shorts/x7Qk2mRw', from: 'youtube.com/watch?v=4b4MUYve_U8',
      screen: '<div class="vid" style="background:linear-gradient(160deg,#ff9f43,#b23a48 60%,#3d1f2c)"></div><p class="h">Try Not To Smile Challenge</p><p class="m">#shorts · 4.1M views · Like · Share · Next</p>', dark: true,
      ans: ['no 0.02', 'single item 0.88', 'entertainment 0.94', 'violates 0.91', 'safe 0.04', 'safe 0.01', 'in scope 0.62', 'out of scope 0.08'],
      verdict: ['popup', 'Steps in: short videos made for endless swiping.'],
      decide: 'shortvideo is far over its threshold, and you came from another video, not a search. Pop-up if you are still here in 4 s.' },
    { site: 'YouTube', c: '#ff0000', label: 'the home page', title: 'YouTube', url: 'youtube.com/', from: 'youtube.com/shorts/x7Qk2mRw',
      screen: '<p class="h">Home</p><p class="m">All · Music · Gaming · Live · Mixes</p><div class="grid"><i></i><i></i><i></i><i></i><i></i><i></i></div>',
      ans: ['no 0.01', 'feed 0.92', 'entertainment 0.81', 'safe 0.06', 'violates 0.83', 'safe 0.02', 'in scope 0.41', 'out of scope 0.12'],
      verdict: ['popup', 'Steps in: a feed, picked for you.'],
      decide: 'The model reads a feed, and the home page is on the feeds rule\'s own list, so it counts even with the model down.' },
    { site: 'Reddit', c: '#ff4500', label: 'a thread from Google', title: 'TypeError: cannot read properties of undefined : r/reactjs', url: 'reddit.com/r/reactjs/comments/1f3k9x', from: 'google.com/search?q=TypeError+cannot+read+properties',
      screen: '<p class="h">TypeError: cannot read properties of undefined (reading \'map\')</p><p class="m">r/reactjs · 3y ago · 41 comments</p><div class="post"><i></i><div><div class="l w90"></div><div class="l w80"></div></div></div><div class="post"><i></i><div><div class="l w60"></div><div class="l w40"></div></div></div>',
      ans: ['no 0.03', 'single item 0.86', 'a task 0.88', 'safe 0.01', 'safe 0.05', 'safe 0.01', 'out of scope 0.03', 'out of scope 0.21'],
      verdict: ['quiet', 'Left alone: opened on purpose, for a task.'],
      decide: 'social is under its threshold, and you arrived from a search, which that rule lets through anyway.' },
    { site: 'Reddit', c: '#ff4500', label: 'r/popular', title: 'Popular posts - Reddit', url: 'reddit.com/r/popular', from: 'reddit.com/r/reactjs/comments/1f3k9x',
      screen: '<p class="h">Popular</p><p class="m">Hot · New · Top · Rising</p><div class="post"><i></i><div><div class="l w80"></div><div class="l w40"></div></div></div><div class="post"><i></i><div><div class="l w90"></div><div class="l w60"></div></div></div><div class="post"><i></i><div><div class="l w60"></div><div class="l w40"></div></div></div>',
      ans: ['no 0.01', 'feed 0.89', 'entertainment 0.77', 'safe 0.03', 'violates 0.77', 'safe 0.01', 'out of scope 0.14', 'in scope 0.71'],
      verdict: ['popup', 'Steps in: a feed, picked for you.'],
      decide: 'The fifth thread opened from a feed is drift. feeds is over its threshold and r/popular is on its list.' },
    { site: 'Netflix', c: '#e50914', label: 'a film', title: 'The Grand Budapest Hotel - Netflix', url: 'netflix.com/watch/70295915', from: 'netflix.com/browse',
      screen: '<div class="vid play" style="background:#2a1f2e"></div><p class="h">The Grand Budapest Hotel</p><p class="m">2014 · 1h 40m · Comedy</p>', dark: true,
      ans: ['no 0.02', 'single item 0.91', 'entertainment 0.95', 'safe 0.02', 'safe 0.03', 'safe 0.01', 'in scope 0.58', 'out of scope 0.04'],
      verdict: ['checkin', 'Checks in: what for, and for how long?'],
      decide: 'videos is a check-in rule, not a block. Netflix is on no list; the sentence "entertainment videos" caught it.' },
    { site: 'Twitch', c: '#9146ff', label: 'a stream', title: 'xQc - Twitch', url: 'twitch.tv/xqc', from: 'twitch.tv/directory',
      screen: '<div class="vid live" style="background:#18141d"></div><p class="h">JUST CHATTING then games</p><p class="m">xQc · 54,210 viewers</p>', dark: true,
      ans: ['no 0.01', 'single item 0.80', 'entertainment 0.96', 'safe 0.05', 'safe 0.11', 'violates 0.96', 'in scope 0.55', 'out of scope 0.10'],
      verdict: ['popup', 'Steps in: an entertainment livestream.'],
      decide: 'livestream is over its threshold. A live lecture would be exempt.' },
    { site: 'Bank', c: '#1b5fb0', label: 'a login', title: 'Sign in - Online Banking', url: 'online.bank.com/login', from: 'google.com/search?q=bank+login',
      screen: '<p class="h">Sign in</p><p class="m">Use your customer number and passcode.</p><div class="form"><div class="l"></div><div class="l"></div><div class="btn"></div></div>',
      ans: ['yes 0.97', 'work 0.71', 'a task 0.90', 'safe 0.00', 'safe 0.01', 'safe 0.00', 'out of scope 0.01', 'out of scope 0.02'],
      verdict: ['private', 'Private. Not acted on, not logged with content.'],
      decide: 'Qualm does nothing, keeps no screenshot, and logs only the site and the score.' },
  ];
  const pick = $('#judge-pick');
  PAGES.forEach((p, i) => {
    const b = document.createElement('button');
    b.type = 'button'; b.setAttribute('role', 'tab'); b.setAttribute('aria-selected', i === 0 ? 'true' : 'false');
    b.innerHTML = '<span class="site" style="background:' + p.c + '">' + p.site[0] + '</span>' + p.site + ', ' + p.label;
    b.addEventListener('click', () => judge(i));
    pick.appendChild(b);
  });
  function judge(i) {
    const p = PAGES[i];
    $$('button', pick).forEach((b, j) => b.setAttribute('aria-selected', j === i ? 'true' : 'false'));
    $('#jm-title').textContent = p.title; $('#jm-url').textContent = p.url;
    const sc = $('#jm-screen'); sc.innerHTML = p.screen; sc.classList.toggle('dark', !!p.dark);
    const v = $('#jm-verdict'); v.className = 'mini-verdict ' + p.verdict[0]; v.textContent = p.verdict[1];
    $('#jm-read').textContent = 'title      ' + p.title + '\nurl        ' + p.url + '\ncame from  ' + p.from + '\ntext       ' + sc.textContent.trim().replace(/\s+/g, ' ').slice(0, 80) + '…';
    const dl = $('#jm-answers'); dl.innerHTML = '';
    RULES.forEach((r, k) => {
      const [ans, num] = p.ans[k].match(/^(.*) ([\d.]+)$/).slice(1);
      const row = document.createElement('div');
      const isHit = r[1] && +num >= r[1] && ['violates', 'yes'].includes(ans);
      const isScope = ans === 'in scope' && +num >= r[1];
      row.className = isHit ? 'hit' : isScope ? 'scope' : '';
      row.innerHTML = '<dt>' + r[0] + '</dt><dd><span class="ans">' + ans + '</span><span class="p" style="--p:' + num + ';--t:' + (r[1] || 0) + ';--tv:' + (r[1] ? 1 : 0) + '"><b></b>' + num + '</span></dd>';
      dl.appendChild(row);
    });
    $('#jm-decision').textContent = p.decide;
  }
  judge(0);

  /* ----------------------------------------------------------- was it worth it */
  const REPLY = { yes: 'Good. Then it did its job by staying out of the way.', some: 'That is most weeks. The closest calls are in Review.', no: 'Noted, without a red number. Review shows where it let it through.' };
  $('#worth').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-w]'); if (!b) return;
    $$('#worth button').forEach((x) => x.setAttribute('aria-pressed', x === b ? 'true' : 'false'));
    $('#worth-reply').textContent = REPLY[b.dataset.w];
  });

  /* ----------------------------------------------------------- the end */
  const end = $('#end');
  const endIo = new IntersectionObserver((es) => { es.forEach((e) => { if (e.intersectionRatio > 0.55) { end.classList.add('on'); document.body.classList.remove('on-paper'); } }); }, { threshold: [0, .55, 1] });
  endIo.observe(end);
})();
