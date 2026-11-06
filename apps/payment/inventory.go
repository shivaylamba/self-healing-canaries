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

// stockV1 is the v1 response: inventory aggregates all warehouses server-side.
type stockV1 struct {
	SKU       string `json:"sku"`
	Available int    `json:"available"`
}

// Available returns the total sellable quantity for sku.
func (c *inventoryClient) Available(ctx context.Context, sku string) (int, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+"/v1/stock/"+sku, nil)
	if err != nil {
		return 0, err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return 0, fmt.Errorf("inventory v1: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("inventory v1: status %d for sku %s", resp.StatusCode, sku)
	}
	var s stockV1
	if err := json.NewDecoder(resp.Body).Decode(&s); err != nil {
		return 0, fmt.Errorf("inventory v1: decode: %w", err)
	}
	return s.Available, nil
}
