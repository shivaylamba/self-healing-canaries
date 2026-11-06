package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
)

type inventoryClient struct {
	baseURL string
	http    *http.Client
}

func newInventoryClient(baseURL string) *inventoryClient {
	return &inventoryClient{
		baseURL: baseURL,
		http:    &http.Client{Timeout: 2 * time.Second, Transport: otelhttp.NewTransport(http.DefaultTransport)},
	}
}

// stockV2 is the v2 response. v1 is deprecated; v2 skips server-side aggregation.
type stockV2 struct {
	SKU       string `json:"sku"`
	Available *int   `json:"available"`
	Warehouses []struct {
		Available int `json:"available"`
	} `json:"warehouses"`
}

// Available returns the total sellable quantity for sku.
func (c *inventoryClient) Available(ctx context.Context, sku string) (int, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+"/v2/stock/"+sku, nil)
	if err != nil {
		return 0, err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return 0, fmt.Errorf("inventory v2: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("inventory v2: status %d for sku %s", resp.StatusCode, sku)
	}
	var s stockV2
	if err := json.NewDecoder(resp.Body).Decode(&s); err != nil {
		return 0, fmt.Errorf("inventory v2: decode: %w", err)
	}
	if s.Available != nil {
		return *s.Available, nil
	}
	if s.Warehouses == nil {
		return 0, fmt.Errorf("inventory v2: response for sku %s has neither 'available' nor 'warehouses' field", sku)
	}
	available := 0
	for _, warehouse := range s.Warehouses {
		available += warehouse.Available
	}
	return available, nil
}
