---
title: Testing i Rust
description: Korleis vi skriv og organiserer testar.
innhaldstype: rettleiing
fagområde: backend
eigarforum: rustforum
tema: testing
teknologi: [Rust]
---

Testar er ein del av koden, ikkje eit tillegg.

## Einingstestar

Einingstestar ligg i same fil som koden, i ein `tests`-modul.

### Eksempel

```rust
#[test]
fn adds() { assert_eq!(2 + 2, 4); }
```

## Integrasjonstestar

Integrasjonstestar ligg i `tests/`-mappa og brukar berre det offentlege API-et.

### Eksempel

Sjå `tests/api.rs` i malprosjektet.

## Sjå [regelen](rules.mdx#rs-203) om unwrap

Testkode kan bruke `unwrap()` fritt.

## Fixtures ##

Delte testdata ligg i `tests/fixtures/`.
