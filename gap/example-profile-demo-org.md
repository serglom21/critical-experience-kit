# Instrumentation profile — `demo`

Window: 7d · scope: all projects

## Spans Without Business Attributes

Custom spans exist but carry no business values, so the funnel is measured in requests rather than outcomes.

| Layer | What we found |
| --- | --- |
| Automatic (SDK-provided) | 10 integration families · 0 attributes |
| Custom business | 6 span names · 0 attributes |
| Code-level (not business) | 9 span names |
| Custom share of span volume | 0.36% |

> A low custom share is normal, not a failure — Heap reports roughly 10% of the events in their own reports are manually tagged. **Zero** is the finding.

### Automatic instrumentation detected

| Integration family | Span volume |
| --- | --- |
| UI rendering | 24,523,024 |
| Browser resources | 2,988,470 |
| Browser timing | 2,737,785 |
| HTTP client (outbound calls) | 1,843,132 |
| HTTP server (inbound requests) | 1,672,838 |
| Database | 1,109,483 |
| Browser page lifecycle | 598,030 |
| Web vitals | 511,050 |
| Unset / default | 260,833 |
| Code-level tracing | 240,189 |

This is the syntactic layer: status codes, durations, query shapes. It cannot tell you what a request *meant* — that boundary is the entire case for custom instrumentation.

### Custom business spans

| Span name | Volume |
| --- | --- |
| `items_added_to_cart` | 120,639 |
| `processCheckout` | 8,567 |
| `User Typing` | 914 |
| `Focus Chat Input` | 911 |
| `Session End: click_agent_button` | 299 |
| `handleApplyPromoCode` | 12 |

### Code-level spans (not business instrumentation)

Real spans, but they name a code location rather than a business step, so they answer *where time went* and not *what the user was trying to do*. Mostly SDK-derived function tracing.

| Span name | Volume |
| --- | --- |
| `src.db.get_products` | 72,214 |
| `src.main.get_api_response_with_caching` | 70,457 |
| `src.db.get_inventory` | 43,020 |
| `src.db.get_products_join` | 40,587 |
| `<unknown>` | 10,153 |
| `src.db.get_promo_code` | 12 |
| `UIKit.NavigationBarContentView.__backButtonAction` | 5 |
| `UIKit.NavigationButtonBar.invalidateAssistant` | 2 |
| `SwiftMessages.MaskingView.tapped` | 1 |

> This split is the one **heuristic** in the profile — pattern-matching on the span description. `source_type` and op families are authoritative; this is not. Check the table before quoting it.

### Customer-defined attributes

**None.** Every attribute in the window was SDK-provided.

## Recommendations

### 1. Custom spans with no business payload  ·  _critical_

6 custom span names are firing with no customer-defined attributes, so the funnel is counted in requests rather than outcomes.

**Ask:** Attach an outcome enum and one numeric magnitude to the spans you already create. Numeric span attributes chart in Trace Explorer with no setup.

---

Automatic vs custom is derived from two signals: `attributeSource.source_type` on `GET /trace-items/attributes/`, and span `op` families from Sentry's documented operation vocabulary. Neither is inferred from naming conventions alone.
