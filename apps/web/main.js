/* TheYgent marketing site — small progressive enhancements, no framework.
   Everything degrades gracefully: with JS off the page is fully readable, and
   under prefers-reduced-motion all motion-driven behaviour early-returns. */
(function () {
  "use strict";

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ── Theme toggle ──────────────────────────────────────────────────────── */
  (function theme() {
    var root = document.documentElement;
    var btn = document.getElementById("themeToggle");
    if (!btn) return;
    var label = btn.querySelector(".theme-label");
    function sync() {
      var t = root.getAttribute("data-theme") || "dark";
      if (label) label.textContent = t;
      btn.setAttribute("aria-label", "Switch to " + (t === "dark" ? "light" : "dark") + " theme");
      // The media-scoped theme-color pair follows the OS scheme; when the stored theme
      // diverges from it, the browser toolbar would keep the wrong color — pin both metas
      // to the active theme. With JS off the media pair still gives correct OS defaults.
      var color = t === "dark" ? "#0b0e14" : "#eef1f6";
      document.querySelectorAll('meta[name="theme-color"]').forEach(function (m) {
        m.setAttribute("content", color);
      });
    }
    sync();
    btn.addEventListener("click", function () {
      var next = (root.getAttribute("data-theme") || "dark") === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try {
        localStorage.setItem("ty-theme", next);
      } catch (e) {}
      sync();
    });
  })();

  /* ── Mobile nav ────────────────────────────────────────────────────────── */
  (function mobileNav() {
    var burger = document.getElementById("navBurger");
    var menu = document.getElementById("navMobile");
    if (!burger || !menu) return;
    function close() {
      menu.hidden = true;
      burger.setAttribute("aria-expanded", "false");
    }
    burger.addEventListener("click", function () {
      var open = burger.getAttribute("aria-expanded") === "true";
      if (open) {
        close();
      } else {
        menu.hidden = false;
        burger.setAttribute("aria-expanded", "true");
      }
    });
    menu.addEventListener("click", function (e) {
      if (e.target.tagName === "A") close();
    });
  })();

  /* ── Binding swap datasheet ────────────────────────────────────────────────
     The page's one interactive centrepiece: the logical id triage-fast stays
     fixed; picking a binding swaps only the binding + "runs on" cell and a note,
     via a short opacity crossfade. Keyboard operable (buttons + arrow keys). */
  (function bindingSwap() {
    var group = document.querySelector(".chips");
    var live = document.querySelector(".ds-live");
    var bindingCell = document.getElementById("dsBinding");
    var runsonCell = document.getElementById("dsRunson");
    var note = document.getElementById("dsNote");
    if (!group || !live || !bindingCell || !runsonCell || !note) return;

    var MAP = {
      mlx: { runson: "Apple Silicon", note: "MLX serves it on Apple Silicon — the Neural Engine and GPU." },
      vllm: { runson: "An NVIDIA GPU", note: "vLLM serves it on an NVIDIA GPU with CUDA batching." },
      llamacpp: { runson: "Any machine", note: "llama.cpp runs it anywhere the binary builds — CPU or GPU." },
      "openai-compatible": {
        runson: "An API you register",
        note: "Reached over HTTP with a key stored on your machine — nothing routed through us."
      }
    };
    var chips = Array.prototype.slice.call(group.querySelectorAll(".chip"));

    function select(chip) {
      var binding = chip.getAttribute("data-binding");
      var data = MAP[binding];
      if (!data) return;

      chips.forEach(function (c) {
        var on = c === chip;
        c.classList.toggle("is-on", on);
        c.setAttribute("aria-checked", on ? "true" : "false");
      });

      function apply() {
        bindingCell.textContent = binding;
        runsonCell.textContent = data.runson;
        note.textContent = data.note;
        live.classList.remove("ds-swapping");
      }
      if (reduceMotion) {
        apply();
      } else {
        live.classList.add("ds-swapping");
        window.setTimeout(apply, 140);
      }
    }

    group.addEventListener("click", function (e) {
      var chip = e.target.closest(".chip");
      if (chip) select(chip);
    });
    // Arrow-key navigation within the radio group.
    group.addEventListener("keydown", function (e) {
      var i = chips.indexOf(document.activeElement);
      if (i < 0) return;
      var next = null;
      if (e.key === "ArrowRight" || e.key === "ArrowDown") next = chips[(i + 1) % chips.length];
      else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = chips[(i - 1 + chips.length) % chips.length];
      if (next) {
        e.preventDefault();
        next.focus();
        select(next);
      }
    });
  })();

  /* ── Copy-to-clipboard ─────────────────────────────────────────────────── */
  (function copy() {
    document.querySelectorAll(".copy-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var text = (btn.getAttribute("data-copy") || "").replace(/&#10;/g, "\n");
        var done = function () {
          var prev = btn.textContent;
          btn.textContent = "Copied";
          btn.classList.add("copied");
          window.setTimeout(function () {
            btn.textContent = prev;
            btn.classList.remove("copied");
          }, 1600);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(done, done);
        } else {
          done();
        }
      });
    });
  })();

  /* ── Video facades ─────────────────────────────────────────────────────────
     Each card is a link to YouTube until it is clicked; the first plain click swaps
     the drawn thumbnail for a youtube-nocookie player in its place. So the page
     makes no request to a video host unless a visitor asks for one, and with JS
     off (or on a modified click) the link still opens the video. */
  (function videos() {
    document.querySelectorAll("a[data-video]").forEach(function (link) {
      link.addEventListener("click", function (e) {
        // Leave new-tab / new-window / download intents to the browser.
        if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        e.preventDefault();
        var frame = document.createElement("iframe");
        frame.src =
          "https://www.youtube-nocookie.com/embed/" + link.getAttribute("data-video") + "?autoplay=1&rel=0";
        frame.title = link.getAttribute("data-title") || "TheYgent walkthrough";
        frame.allow =
          "accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share";
        frame.referrerPolicy = "strict-origin-when-cross-origin";
        frame.setAttribute("allowfullscreen", "");
        var shell = document.createElement("div");
        shell.className = "vid-frame";
        shell.appendChild(frame);
        link.replaceWith(shell);
        frame.focus();
      });
    });
  })();

  /* ── Scroll reveal (opacity crossfade only) ────────────────────────────── */
  (function reveal() {
    var items = document.querySelectorAll(".reveal");
    if (!items.length) return;
    if (reduceMotion || !("IntersectionObserver" in window)) {
      items.forEach(function (el) {
        el.classList.add("in");
      });
      return;
    }
    var io = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            entry.target.classList.add("in");
            io.unobserve(entry.target);
          }
        });
      },
      { rootMargin: "0px 0px -8% 0px", threshold: 0.12 }
    );
    items.forEach(function (el) {
      io.observe(el);
    });
  })();

  /* ── Active nav link on scroll ─────────────────────────────────────────── */
  (function activeNav() {
    var links = Array.prototype.slice.call(document.querySelectorAll(".nav-links a[href^='#']"));
    if (!links.length || !("IntersectionObserver" in window)) return;
    var byId = {};
    links.forEach(function (a) {
      byId[a.getAttribute("href").slice(1)] = a;
    });
    var io = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          var a = byId[entry.target.id];
          if (a && entry.isIntersecting) {
            links.forEach(function (l) {
              l.classList.remove("is-active");
            });
            a.classList.add("is-active");
          }
        });
      },
      { rootMargin: "-45% 0px -50% 0px" }
    );
    Object.keys(byId).forEach(function (id) {
      var sec = document.getElementById(id);
      if (sec) io.observe(sec);
    });
  })();
})();
