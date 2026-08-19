/**
 * Driver: exercises the checkout journey against whatever DSN it is given.
 *
 * The runner points SENTRY_DSN at the local collector, so this needs no Sentry
 * account. CE_VARIANT selects the fixture behaviour, CE_RUNS the instance count,
 * CE_DECLINE_EVERY makes every Nth journey fail so the non-success path — and
 * therefore payment.decline_reason — is actually exercised.
 */
import * as Sentry from "@sentry/node";
import { runCheckout } from "./journey.mjs";

const dsn = process.env.SENTRY_DSN;
if (!dsn) {
  console.error("SENTRY_DSN is required");
  process.exit(2);
}

Sentry.init({ dsn, tracesSampleRate: 1.0, environment: "ce-eval", release: "ce-eval@0" });

const variant = process.env.CE_VARIANT || "correct";
const runs = Number(process.env.CE_RUNS || 6);
const declineEvery = Number(process.env.CE_DECLINE_EVERY || 4);

for (let i = 0; i < runs; i++) {
  const outcome = declineEvery > 0 && (i + 1) % declineEvery === 0 ? "rejected" : "completed";
  await runCheckout({
    variant,
    id: `chk_${String(i).padStart(6, "0")}`,
    outcome,
    cart: {
      total: 129.99 + i,
      items: [{ sku: "a" }, { sku: "b" }],
      currency: "USD",
      planTier: i % 2 ? "enterprise" : "pro",
      paymentMethod: i % 3 ? "card" : "apple_pay",
    },
  });
}

await Sentry.flush(8000);
console.log(JSON.stringify({ variant, runs, declined: Math.floor(runs / declineEvery) }));
