import * as Sentry from "@sentry/node";
import { provider } from "@/lib/payments";

export async function POST(req: Request) {
  const { checkoutId, method } = await req.json();

  // BUG: name drifted. The spec binds this step to `checkout.payment.authorize`,
  // so every query against the contract name returns empty.
  const auth = await Sentry.startSpan(
    {
      name: "checkout.payment_authorize",
      op: "function",
      attributes: {
        "checkout.id": checkoutId,
        "checkout.step": "payment_authorized",
        "payment.method": method,
        "payment.provider": "stripe",
      },
    },
    async (span) => {
      const result = await provider.authorize({ checkoutId, method });
      span.setAttribute("payment.outcome", result.outcome);
      // MISSING: no `payment.decline_reason` on the decline path.
      return result;
    },
  );

  return Response.json(auth);
}
