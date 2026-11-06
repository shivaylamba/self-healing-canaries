package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestInventoryAvailable(t *testing.T) {
	inv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"sku":"sku-1","available":42}`))
	}))
	defer inv.Close()

	got, err := newInventoryClient(inv.URL).Available(context.Background(), "sku-1")
	if err != nil || got != 42 {
		t.Fatalf("Available() = %d, %v; want 42, nil", got, err)
	}
}

func TestInventoryAvailableMultiWarehouse(t *testing.T) {
	inv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"sku":"sku-7","warehouses":[{"id":"sin-1","available":20},{"id":"nrt-1","available":25}]}`))
	}))
	defer inv.Close()

	got, err := newInventoryClient(inv.URL).Available(context.Background(), "sku-7")
	if err != nil || got != 45 {
		t.Fatalf("Available() = %d, %v; want 45, nil", got, err)
	}
}

func TestToMinorUnits(t *testing.T) {
	for _, tc := range []struct {
		amount   float64
		currency string
		want     int64
	}{
		{12.50, "USD", 1250},
		{0.99, "USD", 99},
		{299.99, "EUR", 29999},
		{2900, "JPY", 2900},
		{2900, "jpy", 2900},
		{6100, "KRW", 6100},
	} {
		if got := toMinorUnits(tc.amount, tc.currency); got != tc.want {
			t.Errorf("toMinorUnits(%v, %s) = %d; want %d", tc.amount, tc.currency, got, tc.want)
		}
	}
}

func TestChargeUSD(t *testing.T) {
	c, err := charge(context.Background(), 12.50, "USD")
	if err != nil {
		t.Fatal(err)
	}
	if c.AmountMinor != 1250 {
		t.Fatalf("AmountMinor = %d; want 1250", c.AmountMinor)
	}
}

func TestChargeJPY(t *testing.T) {
	c, err := charge(context.Background(), 2900, "JPY")
	if err != nil {
		t.Fatal(err)
	}
	if c.AmountMinor != 2900 {
		t.Fatalf("AmountMinor = %d; want 2900", c.AmountMinor)
	}
}
