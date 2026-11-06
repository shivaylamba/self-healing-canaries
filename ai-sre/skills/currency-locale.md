---
name: currency-locale
summary: Failures concentrated in one currency, locale, region or unit (minor units, decimals, time zones, number formats).
error_types: [charge_failed, amount_invalid, conversion_failed]
slice_keys: [checkout_currency, checkout_region]
template_regex: (amount|currency|limit|minor|decimal|exceed)
path_globs: [apps/*/money*.go, apps/*/psp*.go, apps/*/currency*.go]
path_kinds: [code]
revert_preference: fix_first
---
# Skill: currency and locale

Typical shape: the same image passes in one environment and fails in another, and every
failure carries one currency or region the passing environment never sent.

1. `compare_environments(image)`: did this exact image pass elsewhere? If so, the bug is in a
   path only some traffic exercises.
2. Group errors by `checkout_currency` and `checkout_region`. Name the dominant value.
3. Compare the environments' traffic: `deploy/overlays/<env>/loadgen.env` on `main`.
4. Find the conversion in the offending change (minor units, rounding, zero-decimal
   currencies such as JPY and KRW, formatting).
5. Fix the conversion for every currency, not only the one seen.
6. Guard, both:
   - a test for zero-decimal and three-decimal currencies;
   - add the missing dimension to staging's traffic profile, so staging sees it next time.
