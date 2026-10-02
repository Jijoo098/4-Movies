/* Amber star rating: stores integer 1–10, displays as 5 stars with half steps */
(function () {
  function ratingToStars(rating) {
    const n = Number(rating) || 0;
    return Math.max(0, Math.min(5, n / 2));
  }

  function starsToRating(stars) {
    return Math.max(1, Math.min(10, Math.round(stars * 2)));
  }

  function paintDisplay(el, rating) {
    const stars = ratingToStars(rating);
    const pct = (stars / 5) * 100;
    el.style.setProperty("--fill", pct + "%");
    el.setAttribute("aria-label", stars ? stars + " out of 5 stars" : "No rating");
    el.title = stars ? stars + " / 5" : "Unrated";
  }

  function buildInteractive(root) {
    const name = root.dataset.name || "rating";
    let value = parseInt(root.dataset.value || "0", 10) || 0;
    let input = root.querySelector('input[type="hidden"]');
    if (!input) {
      input = document.createElement("input");
      input.type = "hidden";
      input.name = name;
      root.appendChild(input);
    }
    input.value = value || "";

    root.innerHTML = "";
    root.appendChild(input);
    root.classList.add("star-rating");

    const row = document.createElement("div");
    row.className = "star-rating__row";
    root.appendChild(row);

    for (let i = 1; i <= 5; i++) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "star-rating__star";
      btn.setAttribute("aria-label", i + " stars");
      btn.innerHTML =
        '<span class="star-rating__half" data-stars="' +
        (i - 0.5) +
        '"></span>' +
        '<span class="star-rating__full" data-stars="' +
        i +
        '"></span>' +
        '<span class="star-rating__glyph">★</span>';
      row.appendChild(btn);
    }

    function paint() {
      const stars = ratingToStars(value);
      row.querySelectorAll(".star-rating__star").forEach((star, idx) => {
        const full = idx + 1;
        star.classList.toggle("is-full", stars >= full);
        star.classList.toggle("is-half", stars >= full - 0.5 && stars < full);
      });
      input.value = value || "";
    }

    row.addEventListener("click", (e) => {
      let stars;
      const target = e.target.closest("[data-stars]");
      if (target) {
        stars = parseFloat(target.dataset.stars);
      } else {
        // Keyboard activation (Enter/Space) lands on the button itself: rate that full star.
        const button = e.target.closest(".star-rating__star");
        if (!button) return;
        stars = Array.prototype.indexOf.call(row.children, button) + 1;
      }
      value = starsToRating(stars);
      paint();
      root.dispatchEvent(new CustomEvent("rating-change", { detail: { rating: value } }));
    });

    paint();
  }

  function init() {
    document.querySelectorAll(".stars-display[data-rating]").forEach((el) => {
      paintDisplay(el, el.dataset.rating);
    });
    document.querySelectorAll(".star-rating[data-name]").forEach(buildInteractive);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.FilmboxedStars = { init, ratingToStars, starsToRating };
})();
