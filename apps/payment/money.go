package main

import (
	"math"
	"strings"
)

// toMinorUnits converts a major-unit amount using the PSP v2 API's ISO 4217 exponent.
func toMinorUnits(amount float64, currency string) int64 {
	switch strings.ToUpper(currency) {
	case "JPY", "KRW":
		return int64(math.Round(amount))
	default:
		return int64(math.Round(amount * 100))
	}
}
