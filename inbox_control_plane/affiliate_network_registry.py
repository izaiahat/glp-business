"""Versioned affiliate-network domain registry shared by inbox classifiers.

This module contains routing metadata only—no credentials, provider clients, or
side effects. Exact domains and subdomains are accepted; substring lookalikes
are rejected by ``network_for_domain``.
"""
from __future__ import annotations

from typing import Final

REGISTRY_VERSION: Final = "affiliate-network-domains-2026-08-26-v8"

NETWORK_BY_DOMAIN: Final[dict[str, str]] = {
    "affiliatinetwork.com": "Affiliati",
    "avantlink.com": "AvantLink",
    "amazon.com": "Amazon Associates",
    "awin.com": "Awin",
    "awin1.com": "Awin",
    "brandchamp.io": "BrandChamp",
    "cj.com": "CJ Affiliate",
    "everflow.io": "Everflow",
    "everflowclient.io": "Everflow",
    "flexoffers.com": "FlexOffers",
    "goaffpro.com": "GoAffPro",
    "gohighlevel.com": "HighLevel Affiliate",
    "healthtrader.com": "HealthTrader",
    "impact.com": "Impact.com",
    "katalys.com": "Katalys",
    "levanta.io": "Levanta",
    "legionsupplements.com": "BrandChamp",
    "linkshare.com": "Rakuten Advertising",
    "maxbounty.com": "MaxBounty",
    "partnerize.com": "Partnerize",
    "partnerstack.com": "PartnerStack",
    "rakutenadvertising.com": "Rakuten Advertising",
    "refersion.com": "Refersion",
    "shareasale.com": "ShareASale",
    "sovrn.com": "Sovrn",
    "ultrahuman.com": "Ultrahuman Affiliate",
    "via-everflow.io": "Everflow",
}


def network_for_domain(sender_domain: str) -> tuple[str, str] | None:
    """Return ``(root_domain, network)`` for an exact domain or subdomain."""
    domain = str(sender_domain or "").strip().lower().rstrip(".")
    for root, network in NETWORK_BY_DOMAIN.items():
        if domain == root or domain.endswith("." + root):
            return root, network
    return None


def network_for_text(text: str) -> tuple[str, str] | None:
    """Return a configured network when text contains a domain token."""
    normalized = str(text or "").lower()
    for root, network in NETWORK_BY_DOMAIN.items():
        if root in normalized:
            return root, network
    return None
