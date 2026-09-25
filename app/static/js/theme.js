/* theme.js — auto/light/dark color mode.
 *
 * The stored choice arrives server-side via the `data-theme-choice` attribute
 * and the POST endpoint via `data-theme-url` (static JS cannot call
 * Django's reverse()). The CSRF token is read from the `csrftoken` cookie
 * (set by the language-switcher form on the page), falling back to the first
 * hidden csrfmiddlewaretoken input.
 *
 * Every choice is additionally persisted in the `dpm_theme` cookie (keep the
 * name in sync with app/core/choices.py): anonymous visitors have no POST
 * endpoint, so without the cookie their toggle would be lost on the next page
 * change and the server would render "auto" again. With the cookie the next
 * response is already server-rendered in the chosen mode — no light flash
 * before this file runs.
 */
(function () {
    "use strict";

    var root = document.documentElement;
    var choice = root.getAttribute("data-theme-choice") || "auto";
    var media = window.matchMedia("(prefers-color-scheme: dark)");
    var COOKIE_NAME = "dpm_theme";

    function systemTheme() {
        return media.matches ? "dark" : "light";
    }

    function apply(theme) {
        var resolved = theme === "auto" ? systemTheme() : theme;
        root.setAttribute("data-bs-theme", resolved);
        // Also drives the UA surfaces (canvas behind the page, scrollbars,
        // form controls) so nothing stays white in dark mode.
        root.style.colorScheme = resolved;
    }

    function storeChoice(value) {
        var secure = window.location.protocol === "https:" ? "; Secure" : "";
        document.cookie = COOKIE_NAME + "=" + encodeURIComponent(value) +
            "; path=/; max-age=31536000; SameSite=Lax" + secure;
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
    // Re-sync the fallback cookie with the server-rendered value (e.g. the
    // preference changed on the settings page since the cookie was written).
    storeChoice(choice);

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
                storeChoice(choice);
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
