/* base.js — confirm dialogs, chip-list count updates, touch-friendly helpers. */
(function () {
    "use strict";

    document.addEventListener("DOMContentLoaded", function () {
        // Confirm dialogs on forms with data-confirm (delete buttons etc.)
        document.querySelectorAll("form[data-confirm]").forEach(function (form) {
            form.addEventListener("submit", function (event) {
                if (!window.confirm(form.dataset.confirm)) {
                    event.preventDefault();
                }
            });
        });

        // Chip-list live count summary
        var countEl = document.querySelector("[data-chip-count]");
        if (countEl) {
            var updateChipCount = function () {
                var n = document.querySelectorAll(".chip-checkbox:checked").length;
                var template = n === 1 ? countEl.dataset.singular : countEl.dataset.plural;
                countEl.textContent = template.replace("#count#", n);
            };
            document.querySelectorAll(".chip-checkbox").forEach(function (box) {
                box.addEventListener("change", updateChipCount);
            });
            updateChipCount();
        }
    });
})();