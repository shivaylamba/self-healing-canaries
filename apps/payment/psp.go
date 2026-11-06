package main

import (
	"context"

	"github.com/chamodshehanka/self-healing-canaries/apps/payment/pspsandbox"
)

// charge authorizes the payment with the PSP (legacy v1 API, major units).
func charge(ctx context.Context, amount float64, currency string) (*pspsandbox.Charge, error) {
	return pspsandbox.ChargeV1(ctx, amount, currency)
}
