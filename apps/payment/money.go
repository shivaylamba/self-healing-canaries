package main

import "math"

// toMinorUnits converts a major-unit amount (12.50) to minor units (1250) for PSP v2.
func toMinorUnits(amount float64) int64 {
	return int64(math.Round(amount * 100))
}
