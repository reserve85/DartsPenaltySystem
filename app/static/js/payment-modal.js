/* payment-modal.js — the shared "Record payment" modal of the financial overview.
 *
 * The overview renders ONE modal for every player row. The row's trigger
 * button carries the player (id + name) and the player's open balance as
 * data-* attributes; this script copies them into the modal while it opens,
 * resets the amount field, fills the "full amount" quick button and focuses
 * the big amount input (opened from a real user gesture, so the on-screen
 * keyboard appears on touch devices).
 */
(function () {
    "use strict";

    document.addEventListener("DOMContentLoaded", function () {
        var modalEl = document.getElementById("paymentModal");
        if (!modalEl || typeof bootstrap === "undefined" || !bootstrap.Modal) {
            return;
        }

        var playerInput = document.getElementById("paymentPlayerId");
        var playerName = document.getElementById("paymentPlayerName");
        var openBalance = document.getElementById("paymentOpenBalance");
        var amountInput = document.getElementById("paymentAmount");
        var fullAmountBtn = document.getElementById("paymentFullAmount");
        var form = modalEl.querySelector("form");

        // Open balance of the row the modal was opened for — needed for the
        // "full amount" quick button.
        var currentBalance = 0;

        // Locale-aware 5.00 / 5,00 formatting (lang comes from <html lang="…">).
        var locale = document.documentElement.lang || "en";
        var money = function (value) {
            try {
                return new Intl.NumberFormat(locale, {
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 2,
                }).format(value);
            } catch (error) {
                return value.toFixed(2);
            }
        };

        modalEl.addEventListener("show.bs.modal", function (event) {
            var trigger = event.relatedTarget;
            if (!trigger) {
                return;
            }
            currentBalance = parseFloat(trigger.dataset.balance || "0") || 0;

            playerInput.value = trigger.dataset.player || "";
            playerName.textContent = trigger.dataset.playerName || "";
            openBalance.textContent = money(currentBalance) + " €";
            amountInput.value = "";

            // "Full amount" only makes sense while something is open — a
            // negative balance is a credit (overpayment), never "pay it all".
            if (currentBalance > 0) {
                fullAmountBtn.hidden = false;
                fullAmountBtn.textContent =
                    (fullAmountBtn.dataset.label || "Full amount") +
                    " (" +
                    money(currentBalance) +
                    " €)";
            } else {
                fullAmountBtn.hidden = true;
            }
        });

        modalEl.addEventListener("shown.bs.modal", function () {
            amountInput.focus();
        });

        // Quick amounts: one tap instead of typing on a phone.
        modalEl.querySelectorAll(".payment-quick-amount").forEach(function (button) {
            button.addEventListener("click", function () {
                if (button.hasAttribute("data-full")) {
                    if (currentBalance <= 0) {
                        return;
                    }
                    // Two decimals keep step="0.01" happy (valueAsNumber === exact).
                    amountInput.value = currentBalance.toFixed(2);
                } else {
                    amountInput.value = button.dataset.amount || "";
                }
                amountInput.focus();
            });
        });

        // Nothing posted but a friendly nudge: the modal stays open with the
        // field highlighted instead of sending an invalid amount.
        form.addEventListener("submit", function (event) {
            var raw = (amountInput.value || "").trim().replace(",", ".");
            if (!/^\d+(\.\d{1,2})?$/.test(raw) || parseFloat(raw) <= 0) {
                event.preventDefault();
                amountInput.reportValidity();
                return;
            }
            // type="number" already stores a dot-decimal value; normalising
            // anyway keeps the POST valid if the browser sent a comma.
            amountInput.value = raw;
        });
    });
})();
