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
		amount float64
		want   int64
	}{{12.50, 1250}, {0.99, 99}, {299.99, 29999}} {
		if got := toMinorUnits(tc.amount); got != tc.want {
			t.Errorf("toMinorUnits(%v) = %d; want %d", tc.amount, got, tc.want)
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
