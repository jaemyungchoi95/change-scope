# Synthetic topology with a callback cycle

Source: cycle, version: 1. Synthetic T05 development fixture, not holdout;
production deployment is unverified.

## HTTP calls

webapi calls answer over HTTP.
answer calls retrieval over HTTP.
retrieval calls reranker over HTTP.
webapi calls suggest over HTTP.
suggest calls reranker over HTTP.

## Callbacks

retrieval calls answer back with ranked passages over HTTP.
