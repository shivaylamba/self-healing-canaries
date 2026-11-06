package main

import (
	"context"

	"github.com/chamodshehanka/self-healing-canaries/apps/payment/pspsandbox"
)

// charge authorizes the payment with the PSP v2 API (minor units).
func charge(ctx context.Context, amount float64, currency string) (*pspsandbox.Charge, error) {
	return pspsandbox.ChargeV2(ctx, toMinorUnits(amount, currency), currency)
}
