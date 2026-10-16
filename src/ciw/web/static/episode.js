/* Episode page: click-to-seek transcript, follow-along highlight, deep links, find-in-page.
   Plain DOM, no dependencies. HTML contract (see tests/test_web_episode_page.py):
     #player              <audio> (absent when the audio file is missing)
     #transcript          container; children .utt[data-start][data-end][id=u<n>] and .gap-marker
     .utt a.ts            timestamp link, href="#t=<seconds>"
     .utt .utt-text       the words (mark.hit = server-side match, span.corr = corrected word)
     a.gap-seek[data-seek]  jump to the start of an untranscribed gap
     #follow #find #find-prev #find-next #find-count   toolbar
   Deep link: /episode/<id>#t=754 seeks (and scrolls) on load, without autoplay. */
(function () {
  'use strict';

  var player = document.getElementById('player');
  var tr = document.getElementById('transcript');
  if (!tr) { return; }

  var utts = Array.prototype.slice.call(tr.querySelectorAll('.utt'));
  var starts = utts.map(function (u) { return parseFloat(u.dataset.start); });
  var follow = document.getElementById('follow');
  var findBox = document.getElementById('find');
  var findCount = document.getElementById('find-count');
  var current = -1;

  /* remembered 'follow' setting (storage can throw in private windows) */
  try {
    var saved = window.localStorage.getItem('ciw-follow');
    if (saved !== null && follow) { follow.checked = saved === '1'; }
  } catch (e) { /* ignore */ }
  if (follow) {
    follow.addEventListener('change', function () {
      try { window.localStorage.setItem('ciw-follow', follow.checked ? '1' : '0'); } catch (e) { /* ignore */ }
    });
  }

  /* index of the last utterance that starts at or before t (-1 before the first) */
  function indexAt(t) {
    var lo = 0, hi = starts.length - 1, ans = -1;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      if (starts[mid] <= t + 0.05) { ans = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    return ans;
  }

  function setCurrent(i, scroll) {
    if (i !== current) {
      if (current >= 0) { utts[current].classList.remove('current'); }
      current = i;
      if (current >= 0) { utts[current].classList.add('current'); }
    }
    if (scroll && current >= 0) {
      utts[current].scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
  }

  function seek(t, play) {
    if (player) {
      var go = function () {
        player.currentTime = t;
        if (play) {
          var p = player.play();
          if (p && p.catch) { p.catch(function () { /* autoplay blocked */ }); }
        }
      };
      if (player.readyState >= 1) { go(); } else { player.addEventListener('loadedmetadata', go, { once: true }); }
    }
    setCurrent(indexAt(t), true);
    syncFind(t);
  }

  function setHash(t) {
    try { history.replaceState(null, '', '#t=' + Math.floor(t)); } catch (e) { /* ignore */ }
  }

  /* --- clicks: timestamps, gap links, or anywhere on an utterance (but not text selection) */
  tr.addEventListener('click', function (e) {
    var gap = e.target.closest('a.gap-seek');
    if (gap) {
      e.preventDefault();
      var g = parseFloat(gap.dataset.seek);
      setHash(g);
      seek(g, true);
      return;
    }
    if (e.target.closest('a:not(.ts)')) { return; }
    var utt = e.target.closest('.utt');
    if (!utt) { return; }
    var onStamp = e.target.closest('a.ts');
    var sel = window.getSelection ? String(window.getSelection()) : '';
    if (!onStamp && sel.length > 0) { return; }  /* the user is selecting text */
    e.preventDefault();
    var t = parseFloat(utt.dataset.start);
    setHash(t);
    seek(t, true);
  });

  if (player) {
    player.addEventListener('timeupdate', function () {
      var i = indexAt(player.currentTime);
      if (i !== current) { setCurrent(i, follow && follow.checked); }
    });
  }

  /* --- deep link #t=<seconds> */
  function applyHash() {
    var m = /^#t=(\d+(?:\.\d+)?)$/.exec(window.location.hash);
    if (m) { seek(parseFloat(m[1]), false); return true; }
    return false;
  }
  window.addEventListener('hashchange', applyHash);

  /* --- space toggles playback unless the user is typing or on a control */
  document.addEventListener('keydown', function (e) {
    if (!player || (e.key !== ' ' && e.code !== 'Space')) { return; }
    if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) { return; }
    var el = e.target;
    var tag = el && el.tagName ? el.tagName.toLowerCase() : '';
    if (tag === 'input' || tag === 'textarea' || tag === 'select' || tag === 'button' ||
        tag === 'audio' || tag === 'a' || (el && el.isContentEditable)) { return; }
    e.preventDefault();
    if (player.paused) {
      var p = player.play();
      if (p && p.catch) { p.catch(function () { /* ignore */ }); }
    } else { player.pause(); }
  });

  /* --- find in transcript: server marks (mark.hit) or client marks (mark.flt) */
  var pos = -1;
  function marks() { return tr.querySelectorAll('mark.hit, mark.flt'); }

  function unmark() {
    Array.prototype.forEach.call(marks(), function (m) {
      var parent = m.parentNode;
      while (m.firstChild) { parent.insertBefore(m.firstChild, m); }
      parent.removeChild(m);
      parent.normalize();
    });
  }

  function escapeRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

  function markTerms(terms) {
    var re = new RegExp(terms.map(escapeRe).join('|'), 'gi');
    var nodes = [];
    var walker = document.createTreeWalker(tr, NodeFilter.SHOW_TEXT, null);
    while (walker.nextNode()) {
      var n = walker.currentNode;
      if (n.parentNode.closest('.utt-text') && re.test(n.nodeValue)) { nodes.push(n); }
      re.lastIndex = 0;
    }
    nodes.forEach(function (node) {
      var text = node.nodeValue, frag = document.createDocumentFragment(), last = 0, m;
      re.lastIndex = 0;
      while ((m = re.exec(text)) !== null) {
        if (m[0].length === 0) { re.lastIndex++; continue; }
        if (m.index > last) { frag.appendChild(document.createTextNode(text.slice(last, m.index))); }
        var mk = document.createElement('mark');
        mk.className = 'flt';
        mk.textContent = m[0];
        frag.appendChild(mk);
        last = m.index + m[0].length;
      }
      if (last < text.length) { frag.appendChild(document.createTextNode(text.slice(last))); }
      node.parentNode.replaceChild(frag, node);
    });
  }

  function show(n) {
    var all = marks();
    if (all.length === 0) { pos = -1; }
    Array.prototype.forEach.call(all, function (m) { m.classList.remove('cur'); });
    if (pos >= 0 && pos < all.length) {
      all[pos].classList.add('cur');
      if (n !== false) { all[pos].scrollIntoView({ block: 'center', behavior: 'smooth' }); }
    }
    if (findCount) {
      var q = findBox ? findBox.value.trim() : '';
      findCount.textContent = !q && all.length === 0 ? '' :
        all.length === 0 ? 'no matches' : (pos >= 0 ? (pos + 1) + ' / ' : '') + all.length + ' match' + (all.length === 1 ? '' : 'es');
    }
  }

  function step(dir) {
    var all = marks();
    if (all.length === 0) { return; }
    pos = pos < 0 ? (dir > 0 ? 0 : all.length - 1) : (pos + dir + all.length) % all.length;
    show(true);
  }

  /* make Next continue from the playback/deep-link position */
  function syncFind(t) {
    var all = marks();
    pos = -1;
    for (var i = 0; i < all.length; i++) {
      var u = all[i].closest('.utt');
      if (u && parseFloat(u.dataset.end) >= t - 0.01) { pos = i; break; }
    }
    show(false);
  }

  var timer = null;
  if (findBox) {
    findBox.addEventListener('input', function () {
      window.clearTimeout(timer);
      timer = window.setTimeout(function () {
        unmark();
        var terms = findBox.value.trim().split(/\s+/).filter(function (s) { return s.length > 0; });
        if (terms.length) { markTerms(terms); }
        pos = -1;
        show(false);
        if (terms.length) { step(1); }
      }, 120);
    });
    findBox.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); step(e.shiftKey ? -1 : 1); }
    });
  }
  var prev = document.getElementById('find-prev');
  var next = document.getElementById('find-next');
  if (prev) { prev.addEventListener('click', function () { step(-1); }); }
  if (next) { next.addEventListener('click', function () { step(1); }); }

  /* --- initial state: a deep link wins; else jump to the first server-side hit */
  if (!applyHash()) {
    if (marks().length > 0) { pos = 0; show(true); }
  }
})();
