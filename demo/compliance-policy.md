# Demo Vendor Compliance Policy

A trimmed policy for the demo sandbox. The agent scans `demo/contracts/` against
these rules and files a finding for any contract that violates them.

## Data residency
- EU customer data must remain in the EU/EEA.
- Violation: any clause permitting processing of EU data in the US/APAC, or silence on residency.

## Data breach notification
- Vendor must notify us within 72 hours of detecting a breach affecting our data.
- Violation: a notice window longer than 72 hours, or no window stated.

## Liability cap
- Vendor liability cap must be at least 12 months of fees paid.
- Violation: a cap below 12 months.
