# Synthetic service topology

Source: topology, version: 1. This document describes a synthetic baseline model;
production deployment is unverified.

## HTTP calls

webapi calls answer over HTTP.
answer calls retrieval over HTTP.
retrieval calls reranker over HTTP.
webapi calls suggest over HTTP.
suggest calls reranker over HTTP.
