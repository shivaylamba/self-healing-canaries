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
		w.Write([]byte(`{"sku":"sku-9","warehouses":[{"id":"sin-1","available":5},{"id":"nrt-1","available":8},{"id":"iad-1","available":40}]}`))
	}))
	defer inv.Close()

	got, err := newInventoryClient(inv.URL).Available(context.Background(), "sku-9")
	if err != nil || got != 53 {
		t.Fatalf("Available() = %d, %v; want 53, nil", got, err)
	}
}

func TestToMinorUnits(t *testing.T) {
	for _, tc := range []struct {
		amount   float64
		currency string
		want     int64
	}{
		{amount: 12.50, currency: "USD", want: 1250},
		{amount: 0.99, currency: "EUR", want: 99},
		{amount: 299.99, currency: "USD", want: 29999},
		{amount: 1000, currency: "JPY", want: 1000},
		{amount: 1000, currency: "KRW", want: 1000},
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
	c, err := charge(context.Background(), 1000, "JPY")
	if err != nil {
		t.Fatal(err)
	}
	if c.AmountMinor != 1000 {
		t.Fatalf("AmountMinor = %d; want 1000", c.AmountMinor)
	}
}
