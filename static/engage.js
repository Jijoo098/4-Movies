/* Recommend / Watchlist toggles on film and series pages.
   Forms with [data-engage-form="recommend|watchlist"] post with fetch() and update in place.
   Without JavaScript (or if fetch fails) the same form simply posts and redirects back. */
(function () {
  "use strict";

  let toastEl = null;
  let toastTimer = null;

  function toast(message) {
    if (!message) return;
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

  // Every toggle of the same kind on the page (e.g. hero button + stat card) shows the same state.
  function apply(kind, data) {
    document.querySelectorAll('form[data-engage-form="' + kind + '"]').forEach(function (form) {
      const btn = form.querySelector("button");
      if (btn) {
        btn.classList.toggle("is-on", !!data.active);
        btn.setAttribute("aria-pressed", data.active ? "true" : "false");
      }
      form.querySelectorAll("[data-engage-value]").forEach(function (el) {
        el.textContent = data.value;
      });
      form.querySelectorAll("[data-engage-label]").forEach(function (el) {
        el.textContent = data.label;
      });
      form.querySelectorAll("[data-engage-sub]").forEach(function (el) {
        el.textContent = data.sub;
      });
      form.querySelectorAll("[data-on]").forEach(function (el) {
        el.textContent = data.active ? el.dataset.on : el.dataset.off;
      });
    });
  }

  document.addEventListener("submit", function (event) {
    const form = event.target.closest ? event.target.closest("form[data-engage-form]") : null;
    if (!form || !window.fetch) return;
    event.preventDefault();
    if (form.dataset.busy === "1") return;
    form.dataset.busy = "1";
    const btn = form.querySelector("button");
    if (btn) btn.disabled = true;

    fetch(form.action, {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-Requested-With": "fetch", Accept: "application/json" },
    })
      .then(function (res) {
        return res.json().then(
          function (data) { return { res: res, data: data }; },
          function () { return { res: res, data: null }; }
        );
      })
      .then(function (out) {
        if (out.res.status === 401 && out.data && out.data.login) {
          window.location.href = out.data.login;
          return;
        }
        if (!out.res.ok || !out.data || !out.data.ok) {
          toast((out.data && out.data.message) || "Something went wrong. Please try again.");
          return;
        }
        apply(form.dataset.engageForm, out.data);
        toast(out.data.message);
      })
      .catch(function () {
        // Network hiccup: fall back to a normal form post (submit() does not fire this handler again).
        form.submit();
      })
      .then(function () {
        form.dataset.busy = "0";
        if (btn) btn.disabled = false;
      });
  });
})();
