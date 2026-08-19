import * as Sentry from "@sentry/node";
import { provider } from "@/lib/payments";

export async function POST(req: Request) {
  const { checkoutId, method } = await req.json();

  const auth = await Sentry.startSpan(
    {
      name: "checkout.payment.authorize",
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
      if (result.outcome !== "authorized") {
        span.setAttribute("payment.decline_reason", result.code);
      }
      return result;
    },
  );

  return Response.json(auth);
}
