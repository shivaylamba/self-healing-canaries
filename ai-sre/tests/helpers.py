"""Shared fixtures-as-functions: log entries shaped like the payment service's Loki output."""

STABLE, CANARY = "6b47474985", "76b6767dd8"


def entry(ts, line, h, error_type=None, **slices):
    labels = {"rollouts_pod_template_hash": h}
    if error_type:
        labels["error_type"] = error_type
    labels.update({f"checkout_{k}": v for k, v in slices.items()})
    return {"ts": ts, "line": line, "labels": labels}


def jpy_entries(t0=1_000_000.0, n=30, h=CANARY):
    return [entry(t0 + i, f"psp: amount {100000 + i} exceeds limit 50000 for JPY", h, "charge_failed",
                  currency="JPY", region="ap-northeast", sku=f"sku-{i % 5}") for i in range(n)]


def inventory_entries(t0=1_000_000.0, n=30, h=CANARY):
    return [entry(t0 + i, f"inventory v2: sku-{7 + i % 3} has no top-level available field", h,
                  "inventory_lookup_failed", currency="USD", region="us-east", sku=f"sku-{7 + i % 3}")
            for i in range(n)]
