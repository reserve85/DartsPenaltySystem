/* payment-modal.js — the shared money modals of the financial overview.
 *
 * TWO modals are wired the same way, marked with [data-money-modal]:
 *   kind "payment" — records money INTO the pot (any positive amount);
 *   kind "payout"  — pays a credit OUT of the pot (capped at the credit,
 *                    enforced server-side by record_payout).
 * Each modal serves EVERY player row: the row's trigger button carries the
 * player (id + name) and the row's balance as data-* attributes; this script
 * copies them into the modal while it opens, resets the amount field, fills
 * the "full amount" quick button and focuses the big amount input (opened
 * from a real user gesture, so the on-screen keyboard appears on touch
 * devices). A negative balance is a CREDIT: it is shown green, the payout
 * modal shows its absolute value, and the "full amount" button fills the
 * debt for a payment and the credit for a payout — never the other way.
 */
(function () {
    "use strict";

    document.addEventListener("DOMContentLoaded", function () {
        if (typeof bootstrap === "undefined" || !bootstrap.Modal) {
            return;
        }

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

        document.querySelectorAll("[data-money-modal]").forEach(function (modalEl) {
            var kind = modalEl.getAttribute("data-money-modal") || "payment";
            var form = modalEl.querySelector("form");
            var playerInput = modalEl.querySelector("[data-money-player]");
            var playerName = modalEl.querySelector("[data-money-player-name]");
            var balanceValue = modalEl.querySelector("[data-money-balance]");
            var amountInput = modalEl.querySelector("[data-money-amount]");
            var fullAmountBtn = modalEl.querySelector("[data-money-full]");
            if (
                !form ||
                !playerInput ||
                !playerName ||
                !balanceValue ||
                !amountInput ||
                !fullAmountBtn
            ) {
                // Defensive: without the cashier the modals never render — nothing to wire.
                return;
            }

            // Balance of the row the modal was opened for — negative = credit.
            var currentBalance = 0;

            modalEl.addEventListener("show.bs.modal", function (event) {
                var trigger = event.relatedTarget;
                if (!trigger) {
                    return;
                }
                currentBalance = parseFloat(trigger.dataset.balance || "0") || 0;

                playerInput.value = trigger.dataset.player || "";
                playerName.textContent = trigger.dataset.playerName || "";
                amountInput.value = "";
                amountInput.setCustomValidity("");

                var hasCredit = currentBalance < 0;
                // The payout field always shows the CREDIT (positive), the
                // payment field the raw balance (negative when overpaid).
                var shown = kind === "payout" ? Math.abs(currentBalance) : currentBalance;
                balanceValue.textContent = money(shown) + " €";
                balanceValue.classList.toggle("text-success", hasCredit);
                balanceValue.classList.toggle("text-danger", !hasCredit);

                // "Full amount" only fills what is actually open — the debt for
                // a payment, the credit for a payout.
                var full =
                    kind === "payout"
                        ? hasCredit
                            ? Math.abs(currentBalance)
                            : 0
                        : currentBalance > 0
                          ? currentBalance
                          : 0;
                if (full > 0) {
                    fullAmountBtn.hidden = false;
                    fullAmountBtn.textContent =
                        (fullAmountBtn.dataset.label || "Full amount") +
                        " (" +
                        money(full) +
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
                        // Two decimals keep step="0.01" happy (valueAsNumber === exact).
                        if (kind === "payout") {
                            if (currentBalance >= 0) {
                                return;
                            }
                            amountInput.value = Math.abs(currentBalance).toFixed(2);
                        } else {
                            if (currentBalance <= 0) {
                                return;
                            }
                            amountInput.value = currentBalance.toFixed(2);
                        }
                    } else {
                        amountInput.value = button.dataset.amount || "";
                    }
                    amountInput.focus();
                });
            });

            amountInput.addEventListener("input", function () {
                amountInput.setCustomValidity("");
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
                // Payout: never send more than the credit (the server re-checks).
                if (kind === "payout" && parseFloat(raw) > Math.abs(currentBalance)) {
                    amountInput.setCustomValidity(
                        form.getAttribute("data-cap-error") ||
                            "The payout exceeds the available credit."
                    );
                    event.preventDefault();
                    amountInput.reportValidity();
                    return;
                }
                // type="number" already stores a dot-decimal value; normalising
                // anyway keeps the POST valid if the browser sent a comma.
                amountInput.value = raw;
            });
        });
    });
})();
