import * as Sentry from "@sentry/react";

export function beginCheckout(cart) {
  const checkoutId = `chk_${crypto.randomUUID()}`;
  sessionStorage.setItem("checkoutId", checkoutId);

  const journey = Sentry.startInactiveSpan({
    name: "checkout",
    op: "ui.action",
    attributes: {
      "checkout.id": checkoutId,
      "checkout.step": "cart_reviewed",
      // BUG: stringified, so sum() and p50() return nothing useful
      "cart.value": cart.total.toFixed(2),
      "cart.item_count": cart.items.length,
      "cart.currency": cart.currency,
      // BUG: boolean collapses failed / abandoned / rejected into one bucket
      "checkout.outcome": false,
      "user.plan_tier": cart.user.planTier,
    },
  });
  Sentry.setActiveSpanInBrowser(journey);
  return { checkoutId, journey };
}

export function submitShipping(checkoutId) {
  return Sentry.startSpan(
    {
      name: "checkout.shipping_submitted",
      op: "ui.action.click",
      attributes: { "checkout.id": checkoutId, "checkout.step": "shipping_submitted" },
    },
    () => postShipping(checkoutId),
  );
}
