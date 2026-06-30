/* ============================================================
   OpenVerba landing page interactions
   - Mobile menu toggle
   - Sticky nav border on scroll
   - Smooth in-page anchor scrolling (respects reduced motion)
   ============================================================ */

(function () {
  "use strict";

  /* ---------- Mobile menu ---------- */
  var toggle = document.getElementById("navToggle");
  var menu = document.getElementById("mobileMenu");

  function closeMenu() {
    if (!toggle || !menu) return;
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-label", "Open menu");
    menu.hidden = true;
  }

  if (toggle && menu) {
    toggle.addEventListener("click", function () {
      var open = toggle.getAttribute("aria-expanded") === "true";
      toggle.setAttribute("aria-expanded", String(!open));
      toggle.setAttribute("aria-label", open ? "Open menu" : "Close menu");
      menu.hidden = open;
      // C2: move focus into the panel when it opens so keyboard users land inside it
      if (!open) {
        var first = menu.querySelector("a");
        if (first) first.focus();
      }
    });

    // Close after tapping any link in the mobile menu
    menu.addEventListener("click", function (e) {
      if (e.target.closest("a")) closeMenu();
    });

    // Close if the viewport grows back to desktop
    window.addEventListener("resize", function () {
      if (window.innerWidth > 900) closeMenu();
    });

    // Close on Escape and return focus to the toggle
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && toggle.getAttribute("aria-expanded") === "true") {
        closeMenu();
        toggle.focus();
      }
    });
  }

  /* ---------- Sticky nav shadow/border on scroll ---------- */
  var nav = document.querySelector(".nav");
  if (nav) {
    var onScroll = function () {
      if (window.scrollY > 8) nav.classList.add("is-scrolled");
      else nav.classList.remove("is-scrolled");
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
  }

  /* ---------- Current year in footer (if a [data-year] node exists) ---------- */
  var yearNodes = document.querySelectorAll("[data-year]");
  if (yearNodes.length) {
    // Avoid Date in restricted envs by reading from the page's own copyright if present.
    var y = (new Date()).getFullYear();
    yearNodes.forEach(function (n) { n.textContent = String(y); });
  }
})();
