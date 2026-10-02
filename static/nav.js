/* 4 Movies — top navbar behaviour (no dependencies). */
(function () {
  'use strict';

  var root = document.documentElement;
  var header = document.getElementById('siteHeader');

  /* ---- Theme (stored in localStorage, default: prefers-color-scheme) ---- */
  var THEME_KEY = 'theme';
  var themeBtn = document.getElementById('themeToggle');

  function storedTheme() {
    try { return localStorage.getItem(THEME_KEY); } catch (e) { return null; }
  }

  function applyTheme(theme) {
    root.dataset.theme = theme;
    if (!themeBtn) return;
    var light = theme === 'light';
    themeBtn.setAttribute('aria-pressed', light ? 'true' : 'false');
    themeBtn.setAttribute('aria-label', light ? 'Switch to dark theme' : 'Switch to light theme');
  }

  var prefersLight = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
  applyTheme(storedTheme() || (prefersLight ? 'light' : 'dark'));

  if (themeBtn) {
    themeBtn.addEventListener('click', function () {
      var next = root.dataset.theme === 'light' ? 'dark' : 'light';
      try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* storage blocked */ }
      applyTheme(next);
    });
  }

  /* ---- Bottom border appears once the page is scrolled past 8px ---- */
  if (header) {
    var onScroll = function () {
      header.classList.toggle('is-scrolled', window.scrollY > 8);
    };
    window.addEventListener('scroll', onScroll, { passive: true });
    onScroll();
  }

  /* ---- Mobile drawer ---- */
  var navToggle = document.getElementById('navToggle');
  var siteNav = document.getElementById('siteNav');

  function setDrawer(open, returnFocus) {
    if (!navToggle || !siteNav) return;
    siteNav.classList.toggle('is-open', open);
    navToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    navToggle.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
    if (!open && returnFocus) navToggle.focus();
  }

  if (navToggle && siteNav) {
    navToggle.addEventListener('click', function () {
      setDrawer(!siteNav.classList.contains('is-open'));
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && siteNav.classList.contains('is-open')) setDrawer(false, true);
    });

    document.addEventListener('click', function (e) {
      if (!siteNav.classList.contains('is-open')) return;
      if (siteNav.contains(e.target) || navToggle.contains(e.target)) return;
      setDrawer(false);
    });

    window.matchMedia('(min-width: 820px)').addEventListener('change', function (e) {
      if (e.matches) setDrawer(false);
    });
  }

  /* ---- "/" focuses the search field (unless already typing somewhere) ---- */
  var searchInput = document.getElementById('navSearchInput');
  document.addEventListener('keydown', function (e) {
    if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey || !searchInput) return;
    var t = e.target;
    var tag = t && t.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (t && t.isContentEditable)) return;
    if (searchInput.offsetParent === null) return; // hidden at this width
    e.preventDefault();
    searchInput.focus();
    searchInput.select();
  });

  /* ---- Search suggestions: trending on focus, live matches while typing ---- */
  (function () {
    if (!searchInput) return;
    var form = searchInput.closest('form');
    if (!form) return;

    var panel = document.createElement('div');
    panel.className = 'search-suggest';
    panel.id = 'navSearchSuggest';
    panel.hidden = true;
    var list = document.createElement('ul');
    list.className = 'search-suggest__list';
    list.setAttribute('role', 'listbox');
    list.id = 'navSearchList';
    panel.appendChild(list);
    var foot = document.createElement('a');
    foot.className = 'search-suggest__all';
    foot.hidden = true;
    panel.appendChild(foot);
    form.appendChild(panel);

    searchInput.setAttribute('role', 'combobox');
    searchInput.setAttribute('aria-autocomplete', 'list');
    searchInput.setAttribute('aria-controls', 'navSearchList');
    searchInput.setAttribute('aria-expanded', 'false');

    var items = [];
    var active = -1;
    var timer = null;
    var controller = null;
    var lastQuery = null;

    function setOpen(open) {
      panel.hidden = !open;
      searchInput.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (!open) setActive(-1);
    }

    function setActive(index) {
      active = index;
      items.forEach(function (el, i) {
        var on = i === index;
        el.classList.toggle('is-active', on);
        el.setAttribute('aria-selected', on ? 'true' : 'false');
        if (on) {
          searchInput.setAttribute('aria-activedescendant', el.id);
          el.scrollIntoView({ block: 'nearest' });
        }
      });
      if (index < 0) searchInput.removeAttribute('aria-activedescendant');
    }

    function render(data) {
      list.textContent = '';
      items = [];
      var results = (data && data.results) || [];
      var query = (data && data.query) || '';

      if (!results.length) {
        var empty = document.createElement('li');
        empty.className = 'search-suggest__empty';
        empty.textContent = query ? 'No matches for \u201c' + query + '\u201d' : 'No suggestions right now';
        list.appendChild(empty);
      } else {
        if (data.trending) {
          var head = document.createElement('li');
          head.className = 'search-suggest__head';
          head.setAttribute('role', 'presentation');
          head.textContent = 'Trending now';
          list.appendChild(head);
        }
        results.forEach(function (r, i) {
          var li = document.createElement('li');
          li.setAttribute('role', 'presentation');
          var a = document.createElement('a');
          a.className = 'search-suggest__item';
          a.href = r.url;
          a.id = 'navSearchOpt' + i;
          a.setAttribute('role', 'option');
          a.setAttribute('aria-selected', 'false');

          var thumb = document.createElement('span');
          thumb.className = 'search-suggest__thumb' + (r.type === 'person' ? ' is-person' : '');
          if (r.image) {
            var img = document.createElement('img');
            img.src = r.image;
            img.alt = '';
            img.width = 40;
            img.height = 60;
            img.loading = 'lazy';
            thumb.appendChild(img);
          } else {
            thumb.textContent = (r.title || '?').charAt(0).toUpperCase();
          }

          var text = document.createElement('span');
          text.className = 'search-suggest__text';
          var title = document.createElement('strong');
          title.textContent = r.title;
          var sub = document.createElement('small');
          sub.textContent = r.subtitle || '';
          text.appendChild(title);
          text.appendChild(sub);

          a.appendChild(thumb);
          a.appendChild(text);
          li.appendChild(a);
          list.appendChild(li);
          items.push(a);
        });
      }

      if (query) {
        foot.hidden = false;
        foot.href = form.action + '?query=' + encodeURIComponent(query);
        foot.textContent = 'See all results for \u201c' + query + '\u201d';
      } else {
        foot.hidden = true;
      }
      setActive(-1);
      setOpen(true);
    }

    function load(query) {
      if (query === lastQuery && !panel.hidden) return;
      lastQuery = query;
      if (controller) controller.abort();
      controller = typeof AbortController === 'function' ? new AbortController() : null;
      fetch('/api/search?q=' + encodeURIComponent(query), controller ? { signal: controller.signal } : undefined)
        .then(function (res) { return res.ok ? res.json() : null; })
        .then(function (data) {
          if (!data || searchInput.value.trim() !== query) return;
          if (document.activeElement !== searchInput) return;
          render(data);
        })
        .catch(function () { /* aborted or offline: keep whatever is showing */ });
    }

    searchInput.addEventListener('focus', function () {
      load(searchInput.value.trim());
    });

    searchInput.addEventListener('input', function () {
      clearTimeout(timer);
      var q = searchInput.value.trim();
      timer = setTimeout(function () { load(q); }, 200);
    });

    searchInput.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        if (!panel.hidden) { setOpen(false); e.preventDefault(); } else { searchInput.blur(); }
        return;
      }
      if (panel.hidden && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
        load(searchInput.value.trim());
        return;
      }
      if (!items.length) return;
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setActive((active + 1) % items.length);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        setActive((active - 1 + items.length) % items.length);
      } else if (e.key === 'Enter' && active >= 0) {
        e.preventDefault();
        window.location.href = items[active].href;
      }
    });

    document.addEventListener('mousedown', function (e) {
      if (!form.contains(e.target)) setOpen(false);
    });

    form.addEventListener('focusout', function () {
      setTimeout(function () {
        if (!form.contains(document.activeElement)) setOpen(false);
      }, 120);
    });
  })();
})();
