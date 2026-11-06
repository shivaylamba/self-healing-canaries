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

func TestChargeUSD(t *testing.T) {
	c, err := charge(context.Background(), 12.50, "USD")
	if err != nil {
		t.Fatal(err)
	}
	if c.AmountMinor != 1250 {
		t.Fatalf("AmountMinor = %d; want 1250", c.AmountMinor)
	}
}
