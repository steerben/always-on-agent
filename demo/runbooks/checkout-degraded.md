# Runbook: Checkout Degraded

**Symptoms:**
- p99 latency on `/checkout` elevated, error rate up
- NullPointerException in `CartService` stack traces

**First checks (in order):**
1. **Recent deploys.** Check `deploys/recent.json` for a `cart-service` deploy in
   the last hour. A deploy regression is the most common cause.
2. **The known NPE bug.** If the stack trace shows `CartService.java:88`, this is
   the unfixed bug where `cart.promoCode` is null for legacy carts created before
   the promo-code feature. Hotfix: null-check at line 88, defaulting to no promo.

**Severity guide:**
- All customers affected → P0, page #checkout-oncall.
- One tenant → P1. Single user → P3.

**Don't:** don't disable checkout globally; roll back the implicated deploy instead.
