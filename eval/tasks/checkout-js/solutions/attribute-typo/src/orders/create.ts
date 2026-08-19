import * as Sentry from "@sentry/node";

export async function createOrder(checkoutId, cart) {
  return Sentry.startSpan(
    {
      name: "checkout.order.create",
      op: "function",
      attributes: {
        "checkoutId": checkoutId,
        "checkout.step": "order_created",
        "cart.currency": cart.currency,
      },
    },
    async (span) => {
      const order = await db.orders.insert(cart);
      span.setAttributes({ "order.id": order.id, "order.value": order.total });
      return order;
    },
  );
}

export async function reserveInventory(checkoutId, cart) {
  return Sentry.startSpan(
    {
      name: "checkout.inventory.reserve",
      op: "function",
      attributes: { "checkoutId": checkoutId, "cart.item_count": cart.items.length },
    },
    async (span) => {
      const res = await inventory.reserve(cart);
      span.setAttribute("inventory.outcome", res.outcome);
      return res;
    },
  );
}
