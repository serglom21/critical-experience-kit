import * as Sentry from "@sentry/react";

export function submitPayment(checkoutId, method) {
  return Sentry.startSpan(
    {
      name: "checkout.payment_submitted",
      op: "ui.action.click",
      attributes: {
        "checkout.id": checkoutId,
        "checkout.step": "payment_submitted",
        "payment.method": method,
      },
    },
    async () => {
      const res = await fetch("/api/checkout/payment", {
        method: "POST",
        body: JSON.stringify({ checkoutId, method }),
      });
      return res.json();
    },
  );
}

// MISSING: no `checkout.confirmation_viewed` span anywhere in the app.
export function showConfirmation(order) {
  return renderConfirmation(order);
}
