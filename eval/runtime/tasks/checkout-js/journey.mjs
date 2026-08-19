import * as Sentry from "@sentry/node";

/**
 * The checkout journey, instrumented per checkout-SPEC.md.
 *
 * Variants exist so the runtime grader can be shown discriminating on the two
 * things static analysis structurally cannot decide:
 *   - `stringified`  cart.value emitted as a string. The source reads
 *                    `cart.total` either way, so eval/grade.py can only say
 *                    "not stringified, type unconfirmed". The wire says number
 *                    vs string, definitively.
 *   - `skip-terminal` the confirmation span exists in the source but is never
 *                    reached on this path — a span that is written but does not
 *                    run. Static analysis sees it and passes.
 */
export async function runCheckout({ variant = "correct", id, cart, outcome = "completed" }) {
  const stringifyValue = variant === "stringified";
  const cartValue = stringifyValue ? cart.total.toFixed(2) : cart.total;

  return await Sentry.startSpan(
    {
      name: "checkout",
      op: "ui.action",
      attributes: {
        "checkout.id": id,
        "checkout.step": "cart_reviewed",
        "cart.value": cartValue,
        "cart.item_count": cart.items.length,
        "cart.currency": cart.currency,
        "checkout.outcome": "abandoned",
        "checkout.entry_point": "cart_page",
        "user.plan_tier": cart.planTier,
      },
    },
    async (journey) => {
      await Sentry.startSpan(
        {
          name: "checkout.shipping_submitted",
          op: "ui.action.click",
          attributes: { "checkout.id": id, "checkout.step": "shipping_submitted" },
        },
        async () => {},
      );

      await Sentry.startSpan(
        {
          name: "checkout.payment_submitted",
          op: "ui.action.click",
          attributes: {
            "checkout.id": id,
            "checkout.step": "payment_submitted",
            "payment.method": cart.paymentMethod,
          },
        },
        async () => {},
      );

      const auth = await Sentry.startSpan(
        {
          name: "checkout.payment.authorize",
          op: "function",
          attributes: {
            "checkout.id": id,
            "checkout.step": "payment_authorized",
            "payment.method": cart.paymentMethod,
            "payment.provider": "stripe",
            "cart.value": cartValue,
          },
        },
        async (span) => {
          const result = outcome === "completed"
            ? { outcome: "authorized" }
            : { outcome: "declined", code: "insuffic_funds" };
          span.setAttribute("payment.outcome", result.outcome);
          if (result.outcome !== "authorized") {
            span.setAttribute("payment.decline_reason", result.code);
          }
          return result;
        },
      );

      if (auth.outcome !== "authorized") {
        journey.setAttributes({
          "checkout.outcome": "rejected",
          "checkout.failure_stage": "payment_authorized",
        });
        return { ok: false };
      }

      await Sentry.startSpan(
        {
          name: "checkout.inventory.reserve",
          op: "function",
          attributes: {
            "checkout.id": id,
            "inventory.outcome": "reserved",
            "cart.item_count": cart.items.length,
          },
        },
        async () => {},
      );

      const order = await Sentry.startSpan(
        {
          name: "checkout.order.create",
          op: "function",
          attributes: {
            "checkout.id": id,
            "checkout.step": "order_created",
            "cart.currency": cart.currency,
          },
        },
        async (span) => {
          const created = { id: `ord_${id.slice(-4)}`, total: cart.total + 4.49 };
          span.setAttributes({ "order.id": created.id, "order.value": created.total });
          return created;
        },
      );

      // The terminal span is written unconditionally in source, but this branch
      // never reaches it in the `skip-terminal` variant.
      if (variant !== "skip-terminal") {
        await Sentry.startSpan(
          {
            name: "checkout.confirmation_viewed",
            op: "ui.action",
            attributes: {
              "checkout.id": id,
              "checkout.step": "confirmation_viewed",
              "order.id": order.id,
              "order.value": order.total,
            },
          },
          async () => {},
        );
      }

      journey.setAttributes({
        "checkout.outcome": "completed",
        "order.id": order.id,
        "order.value": order.total,
      });
      return { ok: true, order };
    },
  );
}
