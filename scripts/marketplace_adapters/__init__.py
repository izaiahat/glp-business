#!/usr/bin/env python3
"""Unified affiliate-marketplace adapter interface.

    from scripts.marketplace_adapters import get, NETWORKS
    net = get("partnerize")
    net.readback(transport)                 # relationship readback
    net.capture_terms(program_id, transport)  # terms -> 0600 capture -> rubric screen
    net.tracking_link(program, subid)       # PURE: no I/O, never fetches a link
    net.apply(program_id, transport, ...)   # only where a join API is documented

Every module in this package implements that four-verb contract and declares
CAPABILITIES + DOCS. Modules are imported lazily so that importing this package
costs nothing and pulls in no HTTP library — the tracking-link resolver depends
on that (it is contractually forbidden from ever fetching a URL).
"""
from __future__ import annotations

import importlib

from ._base import (  # noqa: F401 - re-exported as the adapter toolkit
    CAPABILITY_STATES, NotSupported, RecordingTransport, RequestsTransport, Response,
    json_response, save_terms, screen, utc, write_private,
)

NETWORKS = (
    "awin", "cj", "partnerize", "rakuten", "impact", "healthtrader",
    "clickbank", "digistore24", "partnerstack", "katalys", "refersion",
    "everflow", "sovrn", "levanta", "affiliati", "clickdealer",
    # PROSPECTS — marketplaces the supply-discovery lane found carrying GLP
    # supply we cannot reach, each with a public publisher signup form the
    # portal lane can self-register on. They implement the same four-verb
    # contract and declare every capability "none" until a join and a minted
    # key prove otherwise (see `_prospect.py`).
    "shareasale", "flexoffers", "uppromote", "goaffpro",
)

_CACHE: dict = {}


def get(network: str):
    """Return the adapter module for `network` (lazy import, cached)."""
    key = (network or "").strip().lower()
    if key not in NETWORKS:
        raise KeyError(f"unknown network {network!r}; known: {', '.join(NETWORKS)}")
    if key not in _CACHE:
        _CACHE[key] = importlib.import_module(f"{__name__}.{key}")
    return _CACHE[key]


def preferred_interface(network: str, capability: str = "readback") -> dict:
    """API-key-first routing for one capability (operator standing rule).

    "api"    a working provider API credential exists AND the adapter implements
             the capability against a documented endpoint.
    "portal" no API credential (or no API for this capability) but the portal
             lane drives this marketplace — the machine keeps moving.
    "none"   neither route exists; the finding is the deliverable.

    Import of `portal_lane` is lazy and defensive: routing must never be the
    thing that breaks the daily run.
    """
    key = (network or "").strip().lower()
    state = get(key).CAPABILITIES[capability]
    try:
        from . import portal_lane  # noqa: PLC0415 - lazy, no playwright at import

        has_key = portal_lane.has_api_credential(key)
        portal_supported = key in portal_lane.PORTALS or key in portal_lane.SIGNUP_PORTALS
    except Exception:  # noqa: BLE001
        has_key, portal_supported = False, False
    if state in {"api", "deterministic"} and (has_key or state == "deterministic"):
        route = "api"
    elif portal_supported:
        route = "portal"
    elif state == "deterministic":
        route = "api"
    else:
        route = "none"
    return {"network": key, "capability": capability, "capability_state": state,
            "route": route, "api_credential_present": has_key,
            "portal_lane_supported": portal_supported}


def routing_matrix() -> dict:
    """{network: {capability: route}} — what the machine will actually use."""
    return {net: {cap: preferred_interface(net, cap)["route"]
                  for cap in ("readback", "capture_terms", "tracking_link", "apply")}
            for net in NETWORKS}


def capability_matrix() -> dict:
    """{network: {readback|capture_terms|tracking_link|apply: state}} for receipts."""
    out = {}
    for net in NETWORKS:
        mod = get(net)
        caps = dict(mod.CAPABILITIES)
        assert set(caps) == {"readback", "capture_terms", "tracking_link", "apply"}, net
        assert all(v in CAPABILITY_STATES for v in caps.values()), (net, caps)
        out[net] = caps
    return out


def apply_capable() -> tuple:
    """Networks with an API mutation or deterministic no-application resolution."""
    return tuple(net for net in NETWORKS
                 if get(net).CAPABILITIES["apply"] in {"api", "deterministic"})


def join_adapters() -> dict:
    """Table for affiliate_auto_apply.JOIN_ADAPTERS: {network: (config, candidate) -> receipt}."""
    return {net: get(net).join_adapter for net in apply_capable()}


def tracking_link(network: str, program: dict, subid: str = "site") -> dict:
    """Pure link resolution for one network (no I/O, no fetching, ever)."""
    return get(network).tracking_link(program, subid)


def findings() -> list:
    """Every capability this portfolio genuinely cannot do, with doc citations."""
    rows = []
    for net in NETWORKS:
        mod = get(net)
        for capability, state in mod.CAPABILITIES.items():
            if state in {"api", "deterministic"}:
                continue
            payload = getattr(mod, "APPLY_FINDING", None) if capability == "apply" else None
            payload = payload or getattr(mod, "BLOCKER", None) or getattr(mod, "API_FINDING", None)
            if capability == "capture_terms":
                payload = getattr(mod, "TERMS_FINDING", None) or payload
            if capability == "tracking_link":
                payload = getattr(mod, "LINK_FINDING", None) or payload
            if not payload:
                continue
            rows.append({
                "network": net, "capability": capability, "state": state,
                "reason": payload["reason"], "doc_citations": payload.get("citations", []),
                "fallback_route": payload.get("portal_route"),
            })
    return rows
