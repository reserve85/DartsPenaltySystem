/* cookie-consent.js — essential-cookies notice, shown once per browser. */
(function () {
    "use strict";

    var KEY = "dpm_cookie_consent";

    document.addEventListener("DOMContentLoaded", function () {
        var banner = document.getElementById("cookie-consent");
        if (!banner) {
            return;
        }
        try {
            if (window.localStorage.getItem(KEY)) {
                return;
            }
        } catch (e) {
            // localStorage unavailable (private mode) -> always show the notice.
        }
        banner.classList.remove("d-none");

        var accept = banner.querySelector("[data-cookie-accept]");
        if (accept) {
            accept.addEventListener("click", function () {
                try {
                    window.localStorage.setItem(KEY, "1");
                } catch (e) {
                    // ignore
                }
                banner.classList.add("d-none");
            });
        }
    });
})();
