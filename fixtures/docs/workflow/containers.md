---
title: Konteinarar
description: Bilete, Containerfile og drift.
innhaldstype: tilråding
fagområde: generelt
eigarforum: utviklarforum
tema: konteinerteknologi
teknologi: [Docker/Podman]
prioritet: 2
aktiv: true
ansvarleg: null
pagefind: false
---

## Kort sagt { #samandrag }

Bruk distroless eller slim-bilete, og køyr som ikkje-root.

## Eksempel

```dockerfile
FROM gcr.io/distroless/static
USER nonroot
```
