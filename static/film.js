/* Film detail page behaviour (see the "Film detail page" section in style.css).
   Phase 1: tabs, "..." menu, scroll rails, trailer modal, image fallbacks,
   and light wiring for the existing server-rendered rate / review forms.
   Phase 3 replaces the form wiring with the review modal and JSON API. */
(function () {
  "use strict";

  const root = document.querySelector(".fd");
  if (!root) return;

  const reduceMotion =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const scrollBehavior = reduceMotion ? "auto" : "smooth";
  const TAB_STORAGE_KEY = "fd:tab";

  /* ---------- Toast ---------- */
  let toastEl = null;
  let toastTimer = null;

  function toast(message) {
    if (!toastEl) {
      toastEl = document.createElement("div");
      toastEl.className = "fd-toast";
      toastEl.setAttribute("role", "status");
      toastEl.setAttribute("aria-live", "polite");
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = message;
    toastEl.classList.add("is-visible");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      toastEl.classList.remove("is-visible");
    }, 2200);
  }

  function scrollToEl(el, offset) {
    if (!el) return;
    const top = el.getBoundingClientRect().top + window.pageYOffset - (offset || 96);
    window.scrollTo({ top: Math.max(0, top), behavior: scrollBehavior });
  }

  /* ---------- Rails (cast, crew, similar, posters) ---------- */
  const rails = Array.prototype.slice.call(root.querySelectorAll("[data-fd-rail]"));

  function updateRail(rail) {
    const track = rail.querySelector(".fd-rail__track");
    const prev = rail.querySelector(".fd-rail__btn--prev");
    const next = rail.querySelector(".fd-rail__btn--next");
    if (!track || track.clientWidth === 0) return; // inside a hidden tab panel
    const max = track.scrollWidth - track.clientWidth;
    const hasLeft = track.scrollLeft > 4;
    const hasRight = track.scrollLeft < max - 4;
    rail.classList.toggle("has-more-left", hasLeft);
    rail.classList.toggle("has-more-right", hasRight);
    if (prev) prev.hidden = !hasLeft;
    if (next) next.hidden = !hasRight;
  }

  function updateRails() {
    rails.forEach(updateRail);
  }

  rails.forEach(function (rail) {
    const track = rail.querySelector(".fd-rail__track");
    if (!track) return;
    track.addEventListener("scroll", function () {
      updateRail(rail);
    }, { passive: true });
    const prev = rail.querySelector(".fd-rail__btn--prev");
    const next = rail.querySelector(".fd-rail__btn--next");
    if (prev) {
      prev.addEventListener("click", function () {
        track.scrollBy({ left: -track.clientWidth * 0.8, behavior: scrollBehavior });
      });
    }
    if (next) {
      next.addEventListener("click", function () {
        track.scrollBy({ left: track.clientWidth * 0.8, behavior: scrollBehavior });
      });
    }
  });
  window.addEventListener("resize", updateRails);

  /* ---------- Media card: pill filters + active video ---------- */
  const mediaPills = Array.prototype.slice.call(root.querySelectorAll("[data-fd-media-pill]"));
  mediaPills.forEach(function (pill) {
    pill.addEventListener("click", function () {
      const key = pill.dataset.fdMediaPill;
      mediaPills.forEach(function (other) {
        other.setAttribute("aria-pressed", other === pill ? "true" : "false");
      });
      root.querySelectorAll("[data-fd-media-group]").forEach(function (group) {
        group.hidden = group.dataset.fdMediaGroup !== key;
      });
      updateRails();
    });
  });

  root.addEventListener("click", function (event) {
    const vid = event.target.closest(".fd-vid[data-fd-trailer]");
    if (!vid || !vid.parentElement) return;
    vid.parentElement.querySelectorAll(".fd-vid.is-active").forEach(function (el) {
      el.classList.remove("is-active");
    });
    vid.classList.add("is-active");
  });

  /* ---------- Synopsis: clamp to 5 lines, "Read more" only when the text is actually cut off ---------- */
  const synopsis = root.querySelector("[data-fd-synopsis]");
  const readMore = root.querySelector("[data-fd-readmore]");

  function updateReadMore() {
    if (!synopsis || !readMore) return;
    if (synopsis.clientHeight === 0) return; // Overview tab is hidden; re-checked when it opens
    if (synopsis.classList.contains("is-expanded")) {
      readMore.hidden = false;
      return;
    }
    readMore.hidden = synopsis.scrollHeight <= synopsis.clientHeight + 1;
  }

  if (synopsis && readMore) {
    readMore.addEventListener("click", function () {
      const open = synopsis.classList.toggle("is-expanded");
      readMore.textContent = open ? "Show less" : "Read more";
      readMore.setAttribute("aria-expanded", open ? "true" : "false");
    });
    window.addEventListener("resize", updateReadMore);
  }

  /* ---------- Tabs ---------- */
  const tabs = Array.prototype.slice.call(root.querySelectorAll("[data-fd-tab]"));

  function tabByName(name) {
    return tabs.find(function (tab) {
      return tab.dataset.fdTab === name;
    });
  }

  function activateTab(name, options) {
    const opts = options || {};
    const target = tabByName(name) || tabs[0];
    if (!target) return;
    tabs.forEach(function (tab) {
      const on = tab === target;
      tab.setAttribute("aria-selected", on ? "true" : "false");
      tab.tabIndex = on ? 0 : -1;
      const panel = document.getElementById(tab.getAttribute("aria-controls"));
      if (panel) panel.hidden = !on;
    });
    // The card dashboard under the tabs belongs to the Overview tab only.
    root.querySelectorAll("[data-fd-overview-only]").forEach(function (el) {
      el.hidden = target.dataset.fdTab !== "overview";
    });
    if (opts.focus) target.focus();
    updateReadMore();
    if (opts.updateHash !== false) {
      const hash = target.dataset.fdTab === "overview" ? "" : "#" + target.dataset.fdTab;
      if (hash !== window.location.hash) {
        history.replaceState(null, "", window.location.pathname + window.location.search + hash);
      }
    }
    updateRails();
  }

  const tablist = root.querySelector('[role="tablist"]');
  if (tablist) {
    tablist.addEventListener("click", function (event) {
      const tab = event.target.closest("[data-fd-tab]");
      if (tab) activateTab(tab.dataset.fdTab);
    });
    tablist.addEventListener("keydown", function (event) {
      const index = tabs.indexOf(document.activeElement);
      if (index < 0) return;
      let nextIndex = null;
      if (event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
      else if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
      else if (event.key === "Home") nextIndex = 0;
      else if (event.key === "End") nextIndex = tabs.length - 1;
      if (nextIndex === null) return;
      event.preventDefault();
      activateTab(tabs[nextIndex].dataset.fdTab, { focus: true });
    });
  }

  window.addEventListener("hashchange", function () {
    const name = window.location.hash.slice(1);
    if (tabByName(name)) activateTab(name, { updateHash: false });
  });

  root.querySelectorAll("[data-fd-goto]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      activateTab(btn.dataset.fdGoto);
      scrollToEl(root.querySelector(".fd-tabs"), 84);
    });
  });

  /* ---------- Write a review (Phase 3 swaps this for the modal) ---------- */
  function openReviewForm() {
    activateTab("reviews");
    const form = document.getElementById("fdReviewForm");
    if (!form) return;
    form.hidden = false;
    scrollToEl(form, 120);
    const field = document.getElementById("fdReviewText");
    if (field) field.focus({ preventScroll: true });
  }

  root.querySelectorAll('[data-fd-action="write-review"]').forEach(function (btn) {
    btn.addEventListener("click", openReviewForm);
  });

  /* ---------- "..." menu ---------- */
  root.querySelectorAll("[data-fd-menu]").forEach(function (menu) {
    const toggle = menu.querySelector("[data-fd-menu-toggle]");
    const list = menu.querySelector(".fd-menu__list");
    if (!toggle || !list) return;

    function items() {
      return Array.prototype.slice.call(list.querySelectorAll('[role="menuitem"]')).filter(function (el) {
        return !el.hidden;
      });
    }

    function close(returnFocus) {
      list.hidden = true;
      toggle.setAttribute("aria-expanded", "false");
      if (returnFocus) toggle.focus();
    }

    function open(focusFirst) {
      list.hidden = false;
      toggle.setAttribute("aria-expanded", "true");
      if (focusFirst) {
        const first = items()[0];
        if (first) first.focus();
      }
    }

    toggle.addEventListener("click", function (event) {
      if (list.hidden) open(event.detail === 0); // keyboard-triggered clicks have detail 0
      else close(false);
    });

    toggle.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        open(true);
      }
    });

    list.addEventListener("keydown", function (event) {
      const els = items();
      const index = els.indexOf(document.activeElement);
      if (event.key === "Escape") {
        event.preventDefault();
        close(true);
      } else if (event.key === "ArrowDown") {
        event.preventDefault();
        els[(index + 1) % els.length].focus();
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        els[(index - 1 + els.length) % els.length].focus();
      } else if (event.key === "Tab") {
        close(false);
      }
    });

    document.addEventListener("click", function (event) {
      if (!menu.contains(event.target)) close(false);
    });

    function copyText(text) {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        return navigator.clipboard.writeText(text).then(
          function () { return true; },
          function () { return false; }
        );
      }
      return new Promise(function (resolve) {
        const area = document.createElement("textarea");
        area.value = text;
        area.setAttribute("readonly", "");
        area.style.position = "fixed";
        area.style.opacity = "0";
        document.body.appendChild(area);
        area.select();
        let ok = false;
        try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
        document.body.removeChild(area);
        resolve(ok);
      });
    }

    const copyBtn = menu.querySelector("[data-fd-copy-link]");
    if (copyBtn) {
      copyBtn.addEventListener("click", function () {
        copyText(window.location.href.split("#")[0]).then(function (ok) {
          toast(ok ? "Link copied" : "Could not copy the link");
        });
        close(true);
      });
    }

    const shareBtn = menu.querySelector("[data-fd-share]");
    if (shareBtn && navigator.share) {
      shareBtn.hidden = false;
      shareBtn.addEventListener("click", function () {
        navigator
          .share({ title: document.title, url: window.location.href.split("#")[0] })
          .catch(function () {});
        close(true);
      });
    }
  });

  /* ---------- Image fallbacks (placeholder initials sit behind the photo) ---------- */
  root.querySelectorAll("img[data-fd-avatar]").forEach(function (img) {
    function fail() {
      if (img.parentNode) img.parentNode.removeChild(img);
    }
    img.addEventListener("error", fail, { once: true });
    if (img.complete && img.naturalWidth === 0 && img.getAttribute("src")) fail();
  });

  root.querySelectorAll("img[data-fd-logo]").forEach(function (img) {
    function fail() {
      const title = img.closest(".fd-hero__title");
      if (title) title.classList.remove("has-logo");
      if (img.parentNode) img.parentNode.removeChild(img);
    }
    img.addEventListener("error", fail, { once: true });
    if (img.complete && img.naturalWidth === 0) fail();
  });

  /* ---------- Trailer modal ---------- */
  const modal = document.getElementById("filmTrailerModal");
  const frameHost = document.getElementById("filmTrailerFrame");
  let trailerTrigger = null;

  function youtubeEmbedUrl(url) {
    if (!url) return null;
    try {
      const parsed = new URL(url);
      if (parsed.hostname.indexOf("youtu.be") !== -1) {
        return "https://www.youtube.com/embed/" + parsed.pathname.replace(/^\//, "") + "?autoplay=1&rel=0";
      }
      const id = parsed.searchParams.get("v");
      if (id) return "https://www.youtube.com/embed/" + encodeURIComponent(id) + "?autoplay=1&rel=0";
    } catch (err) {
      return null;
    }
    return null;
  }

  function closeTrailer() {
    if (!modal || modal.hidden) return;
    modal.hidden = true;
    frameHost.textContent = "";
    document.body.style.overflow = "";
    if (trailerTrigger) trailerTrigger.focus();
  }

  function openTrailer(url, trigger) {
    const embed = youtubeEmbedUrl(url);
    if (!modal || !frameHost || !embed) {
      window.open(url, "_blank", "noopener,noreferrer");
      return;
    }
    const iframe = document.createElement("iframe");
    iframe.src = embed;
    iframe.title = "Trailer";
    iframe.allow =
      "accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share";
    iframe.allowFullscreen = true;
    frameHost.textContent = "";
    frameHost.appendChild(iframe);
    trailerTrigger = trigger;
    modal.hidden = false;
    document.body.style.overflow = "hidden";
    const closeBtn = modal.querySelector(".film-trailer-modal__close");
    if (closeBtn) closeBtn.focus();
  }

  root.querySelectorAll("[data-fd-trailer]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      openTrailer(btn.dataset.fdTrailer, btn);
    });
  });

  if (modal) {
    modal.querySelectorAll("[data-trailer-close]").forEach(function (el) {
      el.addEventListener("click", closeTrailer);
    });
    modal.addEventListener("keydown", function (event) {
      if (event.key === "Tab") {
        // The dialog has a single focusable control besides the video; keep focus inside.
        event.preventDefault();
        const closeBtn = modal.querySelector(".film-trailer-modal__close");
        if (closeBtn) closeBtn.focus();
      }
    });
  }

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && modal && !modal.hidden) closeTrailer();
  });

  /* ---------- Rating + review forms (server-rendered in Phase 1) ---------- */
  function wireForms() {
    root.querySelectorAll("[data-fd-rate-form]").forEach(function (form) {
      const widget = form.querySelector(".star-rating");
      if (!widget) return;
      const reviewBox = form.querySelector("[data-fd-rate-review]");
      const hint = form.querySelector("[data-fd-rate-hint]");

      // Saving the review text is a normal submit; it needs a rating.
      form.addEventListener("submit", function (event) {
        const rating = form.querySelector('input[name="rating"]');
        if (!rating || !rating.value) {
          event.preventDefault();
          toast("Pick a star rating first");
        }
      });

      widget.addEventListener("rating-change", function (event) {
        const value = event.detail && event.detail.rating;
        if (!reviewBox) {
          // No review box on this form: keep the original behaviour.
          widget.classList.add("is-saving");
          if (form.requestSubmit) form.requestSubmit();
          else form.submit();
          return;
        }

        // Reveal the optional review box right under the stars.
        reviewBox.hidden = false;
        if (hint && value) {
          hint.textContent = "Your rating: " + (value / 2) + "/5 \u00b7 click a star to change";
        }

        // Save the rating straight away (no page reload) so it is never lost.
        widget.classList.add("is-saving");
        fetch(form.action, {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/x-www-form-urlencoded", "X-Requested-With": "fetch" },
          body: new URLSearchParams(new FormData(form)).toString()
        }).then(function (res) {
          if (!res.ok) throw new Error("status " + res.status);
          if (/\/login/.test(res.url)) {
            window.location.href = res.url;
            return;
          }
          toast("Rating saved");
        }).catch(function () {
          toast("Couldn't save your rating. Please try again.");
        }).then(function () {
          widget.classList.remove("is-saving");
        });
      });
    });

    const reviewForm = document.getElementById("fdReviewForm");
    if (reviewForm) {
      reviewForm.addEventListener("submit", function (event) {
        const rating = reviewForm.querySelector('input[name="rating"]');
        if (!rating || !rating.value) {
          event.preventDefault();
          toast("Pick a star rating first");
          return;
        }
        try { sessionStorage.setItem(TAB_STORAGE_KEY, "reviews"); } catch (err) { /* private mode */ }
      });
    }

    root.querySelectorAll("[data-fd-counter-for]").forEach(function (counter) {
      const field = document.getElementById(counter.dataset.fdCounterFor);
      if (!field) return;
      const max = field.maxLength > 0 ? field.maxLength : 1000;
      function update() {
        counter.textContent = field.value.length + "/" + max;
      }
      field.addEventListener("input", update);
      update();
    });
  }

  /* ---------- Initial state ---------- */
  function restoreInitialTab() {
    const fromHash = window.location.hash.slice(1);
    if (tabByName(fromHash)) {
      activateTab(fromHash, { updateHash: false });
      return;
    }
    let stored = null;
    try {
      stored = sessionStorage.getItem(TAB_STORAGE_KEY);
      sessionStorage.removeItem(TAB_STORAGE_KEY);
    } catch (err) { /* private mode */ }
    if (stored && tabByName(stored)) {
      activateTab(stored);
      // After saving a review, land on it.
      const badge = root.querySelector("#fdReviewList .fd-review__badge");
      const own = badge ? badge.closest(".fd-review") : null;
      if (own) {
        own.classList.add("is-new");
        scrollToEl(own, 120);
      }
    }
  }

  let started = false;

  function init() {
    if (started) return;
    started = true;
    wireForms();
    restoreInitialTab();
    updateRails();
    updateReadMore();
    // Web fonts / images can change line breaks after first paint.
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(updateReadMore);
  }

  // stars.js builds the interactive star widgets on DOMContentLoaded. This file is deferred, so it
  // runs while readyState is "interactive" but before that event fires: register after stars.js
  // (which registered during parsing) so the widgets exist when we wire them up.
  document.addEventListener("DOMContentLoaded", init);
  window.addEventListener("load", init); // safety net if DOMContentLoaded already fired
})();
