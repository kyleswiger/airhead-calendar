# ADR-0001: Hybrid hosting (AWS control plane, local display)

**Status:** accepted (2026-08-01)

## Context

The system must be reachable from phones away from home, and the kitchen display must keep
working during a WAN outage. A Raspberry Pi is the eventual target but none is on hand;
everything else in this account is already AWS + Terraform.

## Decision

Data and agent run in AWS. The display is a browser pointed at the PWA, caching the next 14
days in IndexedDB via a service worker. Writes queue locally and replay on reconnect.

## Consequences

- A blip in home internet dims the display's freshness, not its usefulness.
- Phones work anywhere with no tunnel or dynamic DNS.
- Migrating the display to a Pi is reimaging one device.
- Migrating the *control plane* to a Pi later means running the same FastAPI app under
  uvicorn against SQLite - which is why data access sits behind a repository interface with
  both DynamoDB and SQLite implementations from day one.
- Cost is a few dollars a month rather than zero. Accepted.
