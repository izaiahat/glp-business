#!/usr/bin/env python3
"""Decide whether a program may CREATE a new row on the GLP affiliate Sheet.

Operator, 2026-08-26: "make sure tht the affiliate apply machine isnt fucking
applying to anything OUTSIDE THE US again."

What actually happened was narrower than applying, and worse in a different way.
The Partnerize enumeration mirrors our publisher account's program
relationships into the Sheet. Our account carries relationships with hundreds of
programs that have nothing to do with GLP-1 telehealth, so the sweep wrote rows
like `Netgear Germany`, `Sports Direct DK`, `Evans Cycles UK`, `Indeed AU`,
`Etihad Holidays GCC` and `Advanced System Repair` -- 32 in one drain, kinds
`application_pending` and `new_approval`. Nothing was submitted anywhere. But
the Sheet is the spine of the whole revenue loop: the deploy machine reads it,
the duplicate resolver reads it, and every count we report to the operator comes
off it. Garbage there is not cosmetic.

It had been hidden by an accident: the Sheet was full (778 rows, zero blank), so
those creates were refused for lack of capacity. That backpressure was doing a
job nobody had assigned it. The moment the grid could grow, the junk landed.

So the filter lives HERE, at the single writer, not only at each producer. Every
machine's creates pass through one chokepoint; a rule enforced at the chokepoint
cannot be forgotten by the next producer someone adds.

Two independent reasons to refuse a CREATE:

  * an explicit non-US market marker in the program name;
  * no positive signal that the program belongs to our niche.

Updates to rows that already exist are NOT filtered. This gate only decides
whether the Sheet may grow a new row, so a mistake here can never corrupt or
erase an existing provider relationship.
"""
from __future__ import annotations

import re

#: Explicit non-US market markers. Matched as whole words / suffixes so
#: "Indeed CA" is caught while "CVS Caremark" is not.
NON_US_MARKERS = {
    # country and region names
    "germany", "france", "spain", "denmark", "sweden", "norway", "finland",
    "italy", "netherlands", "belgium", "poland", "portugal", "austria",
    "switzerland", "ireland", "scotland", "wales", "mexico", "brazil",
    "canada", "australia", "new zealand", "japan", "china", "korea", "india",
    "singapore", "malaysia", "indonesia", "thailand", "vietnam",
    "united kingdom", "great britain", "middle east", "emirates", "dubai",
    "saudi", "qatar", "kuwait", "bahrain", "oman", "israel", "turkey",
    "south africa", "nigeria", "kenya", "egypt", "argentina", "chile",
    "colombia", "peru", "europe", "european", "eu", "emea", "apac", "latam",
    "gcc", "nordics", "benelux", "iberia", "dach",
}
#: Two-letter market suffixes, matched only at the END of a name where they are
#: unambiguously a market tag: "Sports Direct DE", "Banbe AU", "Indeed CA".
NON_US_SUFFIXES = {
    "uk", "de", "fr", "es", "it", "nl", "be", "dk", "se", "no", "fi", "pl",
    "pt", "at", "ch", "ie", "ca", "au", "nz", "jp", "cn", "kr", "in", "sg",
    "my", "th", "vn", "br", "mx", "ar", "cl", "co", "za", "ae", "sa", "tr",
    "gr", "cz", "hu", "ro", "il", "hk", "tw", "ph", "eu",
}

#: A program must show at least one of these to earn a new row. Deliberately
#: broad -- this is a niche check, not a quality judgement, and a real provider
#: with an unusual name should reach review rather than be discarded.
NICHE_SIGNALS = {
    "glp", "glp-1", "glp1", "semaglutide", "tirzepatide", "liraglutide",
    "retatrutide", "ozempic", "wegovy", "mounjaro", "zepbound", "saxenda",
    "victoza", "compounded", "peptide",
    "weight", "obesity", "slim", "lean", "metabolic", "metabolism",
    "telehealth", "telemedicine", "teleheath", "virtual care", "online clinic",
    "clinic", "health", "healthcare", "wellness", "med", "meds", "medical",
    "medicine", "pharmacy", "rx", "prescription", "doctor", "md", "care",
    "hormone", "trt", "testosterone", "menopause", "diabetes", "insulin",
    "nutrition", "diet", "supplement", "vitamin", "fitness", "body",
}


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^A-Za-z0-9+]+", name.lower()) if t]


def non_us_marker(program_name: str) -> str | None:
    """Return the marker that makes this look non-US, or None."""
    lowered = " " + re.sub(r"[^a-z0-9 ]+", " ", program_name.lower()) + " "
    for marker in NON_US_MARKERS:
        if f" {marker} " in lowered:
            return marker
    tokens = _tokens(program_name)
    if tokens and tokens[-1] in NON_US_SUFFIXES:
        return tokens[-1].upper()
    return None


def niche_signal(program_name: str) -> str | None:
    """Return the signal that makes this look like our niche, or None."""
    tokens = set(_tokens(program_name))
    lowered = program_name.lower()
    for signal in NICHE_SIGNALS:
        if signal in tokens or (" " in signal and signal in lowered):
            return signal
    return None


def create_verdict(program_name: str, *, fit_verified: bool = False) -> dict[str, object]:
    """May this program CREATE a new Sheet row?

    `fit_verified` means a machine actually assessed this program against our
    niche and recorded the evidence -- the applications machine's fit gate, or
    an operator-queued backlog entry.

    The name alone is NOT a sufficient niche test and must never be used as one.
    Real GLP-1 providers are brandable: Zealthy, Eden, Wisp, Calibrate, Ro,
    Hims & Hers, Precision Telemed. A name-only rule rejected 8 of 18 real
    providers when measured, which would have been a far worse failure than the
    junk it was written to stop -- silently refusing real revenue.

    What actually separated the 32 junk rows from real providers was
    PROVENANCE. They came from a bulk mirror of our whole Partnerize account
    with `evidence: null` and no fit assessment behind any of them. So that is
    what is tested here.

    Returns a verdict dict rather than a bare bool so every refusal is
    recordable and reviewable. A filter whose decisions cannot be audited
    becomes a place where real providers vanish silently.
    """
    name = str(program_name or "").strip()
    if not name:
        return {"allow": False, "reason": "empty_program_name", "detail": ""}

    # Hard stop, and it applies even to fit-assessed candidates: we list US
    # providers. Measured against the real data this caught the non-US junk and
    # flagged zero real providers.
    marker = non_us_marker(name)
    if marker:
        return {"allow": False, "reason": "non_us_market", "detail": marker}

    if fit_verified:
        return {"allow": True, "reason": "fit_verified", "detail": "assessed against the niche"}

    signal = niche_signal(name)
    if signal:
        return {"allow": True, "reason": "niche_signal_in_name", "detail": signal}

    # Neither assessed nor obviously in-niche. Refuse the ROW, keep the lead:
    # this is the bulk-mirror case, and anything real here reaches us again
    # through a path that does assess it.
    return {"allow": False, "reason": "no_fit_assessment", "detail": name[:80]}


def may_create_row(program_name: str, *, fit_verified: bool = False) -> bool:
    return bool(create_verdict(program_name, fit_verified=fit_verified)["allow"])
