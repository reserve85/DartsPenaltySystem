/* theme.js — auto/light/dark color mode.
 *
 * The stored choice arrives server-side via the `data-theme-choice` attribute
 * and the POST endpoint via `data-theme-url` (static JS cannot call
 * Django's reverse()). The CSRF token is read from the `csrftoken` cookie
 * (set by the language-switcher form on the page), falling back to the first
 * hidden csrfmiddlewaretoken input.
 */
(function () {
    "use strict";

    var root = document.documentElement;
    var choice = root.getAttribute("data-theme-choice") || "auto";
    var media = window.matchMedia("(prefers-color-scheme: dark)");

    function systemTheme() {
        return media.matches ? "dark" : "light";
    }

    function apply(theme) {
        root.setAttribute("data-bs-theme", theme === "auto" ? systemTheme() : theme);
    }

    function getCookie(name) {
        var match = document.cookie.match(new RegExp("(^|;\\s*)" + name + "=([^;]*)"));
        return match ? decodeURIComponent(match[2]) : null;
    }

    function csrfToken() {
        var fromCookie = getCookie("csrftoken");
        if (fromCookie) {
            return fromCookie;
        }
        var input = document.querySelector("input[name=csrfmiddlewaretoken]");
        return input ? input.value : "";
    }

    function markActive() {
        document.querySelectorAll("[data-theme-choice-btn]").forEach(function (btn) {
            btn.classList.toggle("active", btn.getAttribute("data-theme-choice-btn") === choice);
        });
    }

    // Initial server-rendered choice -> resolved Bootstrap color mode.
    apply(choice);

    // Follow the OS preference while the choice is "auto".
    media.addEventListener("change", function () {
        if (choice === "auto") {
            apply("auto");
        }
    });

    document.addEventListener("DOMContentLoaded", function () {
        markActive();
        document.querySelectorAll("[data-theme-choice-btn]").forEach(function (btn) {
            btn.addEventListener("click", function () {
                choice = btn.getAttribute("data-theme-choice-btn");
                root.setAttribute("data-theme-choice", choice);
                apply(choice);
                markActive();

                var url = root.getAttribute("data-theme-url");
                if (!url) {
                    return; // anonymous: session-only visual toggle
                }
                var body = new FormData();
                body.append("theme", choice);
                fetch(url, {
                    method: "POST",
                    credentials: "same-origin",
                    headers: {
                        "X-CSRFToken": csrfToken(),
                        "X-Requested-With": "XMLHttpRequest"
                    },
                    body: body
                });
            });
        });
    });
})();
