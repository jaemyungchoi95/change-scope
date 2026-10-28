# Synthetic service topology for review

Source: topology-review, version: 2. This document describes a synthetic baseline model;
production deployment is unverified.

## HTTP calls

webapi calls answer over HTTP.
answer calls retrieval over HTTP.
retrieval calls reranker over HTTP.
webapi calls suggest over HTTP.
suggest serves cached results and no longer calls reranker.

## Batch

webapi calls answer for the nightly batch export.

## Operations

Operators restart reranker during maintenance windows.
