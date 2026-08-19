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

export function showConfirmation(order, checkoutId) {
  return Sentry.startSpan(
    {
      name: "checkout.confirmation_viewed",
      op: "ui.action",
      attributes: {
        "checkout.id": checkoutId,
        "checkout.step": "confirmation_viewed",
        "order.id": order.id,
      },
    },
    () => {
      const active = Sentry.getActiveSpan();
      const root = active ? Sentry.getRootSpan(active) : undefined;
      root?.setAttributes({ "checkout.outcome": "completed", "order.value": order.total });
      return renderConfirmation(order);
    },
  );
}
