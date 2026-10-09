#!/usr/bin/env python3
"""Materialize exact apply/review jobs for one operator-GO packet."""
from __future__ import annotations
import argparse, datetime as dt, hashlib, html, json, os, re, subprocess, sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from current_content_base import verify_resolution  # noqa: E402
from deterministic_qa_repair import run_bounded_qa_repair  # noqa: E402
from existing_article_updates import validate_r2_execution_contract  # noqa: E402
from machine_config import load_config  # noqa: E402
from r2_execution_control import surface_spec  # noqa: E402
from r2_update_carrier import validate_update_authority  # noqa: E402
from selection_receipt import (  # noqa: E402
    load_selection_receipt,
    selection_binding,
    selection_spec,
)
import review_transports  # noqa: E402

BUSINESS = Path("/home/ubuntu/business")
SYSTEM = BUSINESS / "systems/seo-blog-machine"
QUEUE = BUSINESS / "reports/glp-clinic-scout/seo-blog-machine-20260730/topic-queues/latest.json"
BASE_STATE = BUSINESS / "reports/glp-clinic-scout/seo-machine/current-content-base.json"
PUBLISHED_LEDGER = BUSINESS / "reports/glp-clinic-scout/seo-machine/published-topic-ledger.json"
UPDATE_EVENT_LEDGER = BUSINESS / "reports/glp-clinic-scout/seo-machine/article-update-event-ledger.json"
PYTHON = Path("/usr/bin/python3.12")
CONFIG = SYSTEM / "config/verticals/glp-clinic-scout.json"
ROUTE_PATTERN = re.compile(r"^/(guides|best|reviews|cost|tools)/([a-z0-9]+(?:-[a-z0-9]+)*)/$")
# repair_opus_packet.py owns a 2400-second worker budget. The review controller
# must outlive that worker long enough to reap its process group and persist the
# execution receipt; a shorter outer timeout silently recreates starvation.
REPAIR_CONTROLLER_TIMEOUT_SECONDS = 2430


def derive_route_paths(route: str) -> dict[str, str]:
    match = ROUTE_PATTERN.fullmatch(route)
    if not match:
        raise RuntimeError("canonical_route_invalid")
    namespace, slug = match.groups()
    return {
        "namespace": namespace,
        "slug": slug,
        "target_public_rel": f"{namespace}/{slug}/index.html",
        "section_index_rel": f"{namespace}/index.html",
        "visual_key": f"guide-{slug}",
    }


def author_profile_public_path(article: dict[str, Any]) -> str:
    """Return the exact generated author page owned by one article byline."""
    bio_path = str((article.get("author") or {}).get("bio_path") or "")
    match = re.fullmatch(r"/authors/([a-z0-9]+(?:-[a-z0-9]+)*)/", bio_path)
    if match is None:
        raise RuntimeError(f"article_author_bio_path_invalid:{bio_path}")
    return f"authors/{match.group(1)}/index.html"


def author_profile_public_changes(
    current_article: dict[str, Any], replacement_article: dict[str, Any],
) -> list[str]:
    """Declare author pages only when an R2 replacement changes its byline."""
    current = author_profile_public_path(current_article)
    replacement = author_profile_public_path(replacement_article)
    return [] if current == replacement else sorted({current, replacement})


def existing_section_index_changes(base_release_root: Path,
                                   route_parts: dict[str, str]) -> list[str]:
    """Return the section index only when accepted base truth actually owns it.

    Some canonical namespaces (currently ``/best/``) have article routes but no
    section landing page.  Delta authority must never claim a nonexistent file
    will change.  Conversely, an existing entry must be an ordinary canonical
    file, not a symlink or other filesystem alias.
    """
    base = base_release_root.resolve(strict=True)
    if not base.is_dir() or base_release_root.is_symlink():
        raise RuntimeError("base_release_root_invalid")
    relative = Path(route_parts["section_index_rel"])
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != route_parts["section_index_rel"]:
        raise RuntimeError("section_index_path_invalid")
    section = base / "public" / relative
    if not os.path.lexists(section):
        return []
    if section.is_symlink() or not section.is_file() or section.resolve() != section:
        raise RuntimeError("section_index_entry_invalid")
    return [relative.as_posix()]


def article_card_projection(article: dict[str, Any]) -> tuple[str, str, str]:
    """Fields that the generated Insights/related-card templates actually render."""
    route = str(article.get("canonical_path") or "")
    title = str(article.get("h1") or article.get("title") or "Clinic Scout source-checked guide")
    summary = str(
        article.get("meta_description")
        or article.get("direct_answer")
        or "Source-checked Clinic Scout research."
    )
    if not route.startswith("/") or not route.endswith("/"):
        raise RuntimeError(f"article_card_route_invalid:{route}")
    return route, title, summary


def article_links_to_route(article: dict[str, Any], target_route: str) -> bool:
    """Return whether any existing catalog paragraph already links to a route."""
    for section in article.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for paragraph in section.get("paragraphs") or []:
            if target_route in str(paragraph):
                return True
    return False


def unsatisfied_inbound_public_changes(
    current_articles: list[dict[str, Any]], inbound_plan: list[dict[str, Any]],
) -> list[str]:
    """Declare only source pages that still need the packet's inbound link."""
    changes: set[str] = set()
    for plan in inbound_plan:
        source_route = str(plan.get("source_route") or "")
        target_route = str(plan.get("target_route") or "")
        sources = [row for row in current_articles if row.get("canonical_path") == source_route]
        if len(sources) != 1:
            raise RuntimeError(f"inbound_link_source_exact_match_invalid:{source_route}:{len(sources)}")
        if not article_links_to_route(sources[0], target_route):
            changes.add(f"{source_route.strip('/')}/index.html")
    return sorted(changes)


def related_article_card_public_paths(
    base_release_root: Path,
    current_article: dict[str, Any],
    replacement_article: dict[str, Any],
) -> list[str]:
    """Find exact existing pages whose rendered card bytes depend on an R2 article.

    The Phase 3 build embeds article title/summary cards on Insights, commercial,
    and other article pages. A refresh of an early catalog row can therefore
    truthfully change dozens of generated routes even though only one article is
    being edited. Discover those consumers from accepted public bytes instead of
    maintaining a second renderer graph or widening lastmod provenance globally.
    """
    old_route, old_title, old_summary = article_card_projection(current_article)
    new_route, new_title, new_summary = article_card_projection(replacement_article)
    if new_route != old_route:
        raise RuntimeError("r2_article_card_route_changed")
    changed_tokens = {
        html.escape(old, quote=True)
        for old, new in ((old_title, new_title), (old_summary, new_summary))
        if old != new
    }
    if not changed_tokens:
        return []

    base = base_release_root.resolve(strict=True)
    public = base / "public"
    if base_release_root.is_symlink() or not base.is_dir() or not public.is_dir():
        raise RuntimeError("base_release_root_invalid")
    anchor = re.compile(
        rf'<a\b[^>]*\bhref="{re.escape(html.escape(old_route, quote=True))}"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    consumers: list[str] = []
    for path in sorted(public.rglob("index.html")):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"article_card_consumer_entry_invalid:{path}")
        source = path.read_text(encoding="utf-8")
        if any(any(token in body for token in changed_tokens) for body in anchor.findall(source)):
            consumers.append(path.relative_to(public).as_posix())
    return consumers


def new_article_insights_public_delta(
    base_release_root: Path,
    target_route: str,
) -> dict[str, list[str] | int]:
    """Declare the exact Insights pagination delta caused by an appended guide.

    New articles are appended to ``content-batch.json``, BEFORE national rows.
    Derive the insertion point from the accepted rendered card order and its
    catalog ownership, not from the final page's spare capacity. If the page
    count grows every pagination control changes; otherwise only the pages
    at/after insertion change. Dates and unrelated routes are never rewritten.
    """
    if not target_route.startswith("/guides/"):
        return {"added": [], "changed": [], "page_size": 0}

    base = base_release_root.resolve(strict=True)
    public = base / "public"
    renderer = base / "src/phase3-page-graph.mjs"
    if base_release_root.is_symlink() or not base.is_dir() or not public.is_dir():
        raise RuntimeError("base_release_root_invalid")
    if renderer.is_symlink() or not renderer.is_file() or renderer.resolve() != renderer:
        raise RuntimeError("insights_renderer_invalid")
    page_size_matches = re.findall(
        r"renderPhase3InsightsPage\(articles, pageNumber = 1, pageSize = (\d+)\)",
        renderer.read_text(encoding="utf-8"),
    )
    if len(page_size_matches) != 1 or int(page_size_matches[0]) < 1:
        raise RuntimeError(f"insights_page_size_contract_invalid:{page_size_matches}")
    page_size = int(page_size_matches[0])

    pages: dict[int, Path] = {1: public / "insights/index.html"}
    for path in sorted((public / "insights/page").glob("*/index.html")):
        try:
            number = int(path.parent.name)
        except ValueError as error:
            raise RuntimeError(f"insights_page_number_invalid:{path.parent.name}") from error
        if number < 2 or number in pages:
            raise RuntimeError(f"insights_page_number_invalid:{number}")
        pages[number] = path
    expected_numbers = list(range(1, max(pages) + 1))
    if sorted(pages) != expected_numbers:
        raise RuntimeError(f"insights_page_sequence_invalid:{sorted(pages)}")

    counts: dict[int, int] = {}
    card_routes: list[str] = []
    feature = re.compile(r"<a\b[^>]*\bdata-insights-feature\b", re.IGNORECASE)
    story = re.compile(r'<a\b[^>]*class="[^"]*\bp3-story\b[^"]*"', re.IGNORECASE)
    for number, path in sorted(pages.items()):
        if path.is_symlink() or not path.is_file() or path.resolve() != path:
            raise RuntimeError(f"insights_page_entry_invalid:{path}")
        source = path.read_text(encoding="utf-8")
        counts[number] = len(feature.findall(source)) + len(story.findall(source))
        for anchor in re.findall(r"<a\b[^>]*>", source, re.IGNORECASE):
            if not (feature.match(anchor) or story.match(anchor)):
                continue
            hrefs = re.findall(r'\bhref="([^"]+)"', anchor)
            if len(hrefs) != 1:
                raise RuntimeError("insights_card_href_invalid")
            card_routes.append(html.unescape(hrefs[0]))
    last_number = max(pages)
    invalid_counts = {
        number: count
        for number, count in counts.items()
        if count < 1 or count > page_size or (number < last_number and count != page_size)
    }
    if invalid_counts:
        raise RuntimeError(f"insights_page_card_count_invalid:{invalid_counts}")

    catalogs = []
    for filename in ("content-batch.json", "content-national-wow-additions.json"):
        path = base / "data/blog" / filename
        if path.is_symlink() or not path.is_file() or path.resolve() != path:
            raise RuntimeError("insights_catalog_invalid:" + filename)
        payload = json.loads(path.read_text(encoding="utf-8"))
        catalogs.append([row["canonical_path"] for row in payload["articles"]])
    all_routes = catalogs[0] + catalogs[1]
    if (len(set(all_routes)) != len(all_routes) or
            len(set(card_routes)) != len(card_routes) or
            target_route in all_routes):
        raise RuntimeError("insights_catalog_route_collision")
    # Project actual rendered eligibility instead of duplicating vertical and
    # exclusion filters. Reject drift in catalog order or unknown public cards.
    visible = set(card_routes)
    if [route for route in all_routes if route in visible] != card_routes:
        raise RuntimeError("insights_catalog_card_order_mismatch")
    insertion = sum(route in visible for route in catalogs[0])
    first_changed = insertion // page_size + 1
    relative = lambda path: path.relative_to(public).as_posix()
    if counts[last_number] < page_size:
        return {
            "added": [],
            "changed": [relative(pages[number]) for number in expected_numbers
                        if number >= first_changed],
            "page_size": page_size,
        }
    next_path = f"insights/page/{last_number + 1}/index.html"
    return {
        "added": [next_path],
        "changed": [relative(pages[number]) for number in expected_numbers],
        "page_size": page_size,
    }


def validate_unused_release_root(release_root: Path, base_release_root: Path,
                                 flagship_root: Path) -> Path:
    """Require a fresh sibling candidate; never alias or overwrite accepted truth."""
    release = release_root.resolve()
    base = base_release_root.resolve()
    expected_parent = flagship_root.resolve() / "releases"
    if release.parent != expected_parent:
        raise RuntimeError("release_root_parent_invalid")
    if release == base:
        raise RuntimeError("release_root_aliases_current_base")
    if os.path.lexists(release):
        raise RuntimeError("release_root_exists")
    return release


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic(path: Path, value: dict[str, Any]) -> None:
    text = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text() != text:
            raise RuntimeError(f"materialized_job_drift:{path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temp.write_text(text); os.chmod(temp, 0o600); os.replace(temp, path)


def spec(path: Path) -> dict[str, str]:
    path = path.resolve(); return {"path": str(path), "sha256": sha(path)}


def executable(argv: list[Path | str], controls: list[Path], timeout: int, **extra: Any) -> dict[str, Any]:
    value = {"argv": [str(item) for item in argv], "executable": str(PYTHON),
             "executable_sha256": sha(PYTHON),
             "control_files": {str(path.resolve()): sha(path) for path in controls}, "timeout": timeout}
    value.update(extra); return value


def validate_packet_authority(authority: dict[str, Any], expected_topic_id: str,
                              expected_query: str, expected_route: str) -> dict[str, Any]:
    ids = [authority.get("topic_id"), authority.get("requested_queue_item_id"),
           authority.get("observed_queue_topic_id"),
           (authority.get("queue_authority") or {}).get("topic_id")]
    bound_ids = [str(value) for value in ids if value]
    if not bound_ids or any(value != expected_topic_id for value in bound_ids):
        raise RuntimeError("packet_topic_id_mismatch")
    routes = [authority.get("route"), (authority.get("target") or {}).get("route")]
    if any(str(value) != expected_route for value in routes if value):
        raise RuntimeError("packet_route_mismatch")
    queries = [authority.get("topic"), (authority.get("target") or {}).get("query"),
               (authority.get("queue_authority") or {}).get("target_query")]
    normalize = lambda value: " ".join(str(value).lower().split())
    if any(normalize(value) != normalize(expected_query) for value in queries if value):
        raise RuntimeError("packet_query_mismatch")
    return {"topic_id": expected_topic_id, "target_query": expected_query, "target_route": expected_route}


def r2_materialization_subject(authority: dict[str, Any]) -> dict[str, Any]:
    required_contract = {
        "schema": "glp-seo-r2-update-authority-v1",
        "status": "AUTHORIZED_R2_REFRESH",
        "operation": "r2_refresh",
        "refresh_tier": "R2",
    }
    if any(authority.get(key) != value for key, value in required_contract.items()):
        raise RuntimeError("r2_update_authority_not_proven")
    subject = {
        key: authority.get(key)
        for key in ("topic_id", "article_id", "target_query", "target_route", "r2_contract")
    }
    if any(
        not isinstance(subject[key], str) or not subject[key].strip()
        for key in ("topic_id", "article_id", "target_query", "target_route")
    ) or not isinstance(subject["r2_contract"], dict):
        raise RuntimeError("r2_update_authority_subject_invalid")
    return subject


def bind_packet_selection(packet: Path, receipt_path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    receipt, selected = load_selection_receipt(receipt_path)
    receipt_spec = selection_spec(receipt_path)
    expected_binding = selection_binding(receipt)
    authority_path = packet / "00-authority.json"
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    if authority.get("selection_receipt") not in (None, receipt_spec):
        raise RuntimeError("packet_selection_receipt_mismatch")
    if authority.get("selection_binding") not in (None, expected_binding):
        raise RuntimeError("packet_selection_binding_mismatch")
    authority["selection_receipt"] = receipt_spec
    authority["selection_binding"] = expected_binding
    temporary = authority_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(authority, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(authority_path)
    return receipt, selected, receipt_spec


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", required=True); parser.add_argument("--topic-id", required=True)
    parser.add_argument("--route", required=True); parser.add_argument("--run-id", required=True)
    parser.add_argument("--release-root", required=True); parser.add_argument("--publication-date", required=True)
    parser.add_argument("--selection-receipt", type=Path, required=True)
    parser.add_argument("--topic-authority-snapshot", type=Path)
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--r2-authority", type=Path)
    parser.add_argument("--operation", choices=("new_article", "r2_refresh"), default="new_article")
    parser.add_argument("--update-event-ledger", type=Path, default=UPDATE_EVENT_LEDGER)
    parser.add_argument("--pt-date")
    parser.add_argument(
        "--review-transport",
        default=review_transports.DEFAULT_TRANSPORT_ID,
        choices=tuple(review_transports.AUTHORIZED_TRANSPORTS),
        help="Authorized review transport for this job (operator dual authorization 2026-08-18).",
    )
    parser.add_argument(
        "--materialization-reason",
        choices=("new_article_mint", "r2_refresh", "review_relaunch", "recovery", "remint", "fixture"),
        default="new_article_mint",
    )
    args = parser.parse_args()
    if args.operation == "r2_refresh" and args.r2_authority is None:
        raise RuntimeError("r2_authority_required")
    if args.operation == "new_article" and args.r2_authority is not None:
        raise RuntimeError("r2_authority_only_allowed_for_r2_refresh")
    if args.operation == "r2_refresh" and args.materialization_reason != "r2_refresh":
        raise RuntimeError("r2_materialization_reason_required")
    if args.operation == "new_article" and args.materialization_reason == "r2_refresh":
        raise RuntimeError("new_article_materialization_reason_invalid")
    if args.materialization_reason == "remint" and args.topic_authority_snapshot is None:
        raise RuntimeError("remint_topic_authority_snapshot_required")
    if args.topic_authority_snapshot is not None and args.materialization_reason != "remint":
        raise RuntimeError("topic_authority_snapshot_only_allowed_for_remint")
    packet = Path(args.packet).resolve(); run_root = packet.parent
    route_parts = derive_route_paths(args.route)
    if not packet.is_dir():
        raise RuntimeError("packet_or_route_invalid")
    # Validate the candidate target before bind_packet_selection or any run-scoped
    # artifact write. This prevents a reviewed packet from emitting an apply job
    # that aliases the accepted predecessor or targets an existing release.
    config = load_config(args.config)
    publishing = config["publishing"]
    legacy_report_only = (publishing.get("auto_generate") is False
                          and publishing.get("auto_publish") is False)
    autonomous = (
        publishing.get("auto_generate") is True
        and publishing.get("auto_publish") is True
        and publishing.get("approval_mode") == "AUTONOMOUS_MACHINE_GATES"
        and isinstance(publishing.get("authority_receipt"), str)
        and Path(publishing["authority_receipt"]).is_file()
    )
    # Autonomous release per the standing operator authority receipt
    # (reports/glp-clinic-scout/seo-machine/OPERATOR-AUTHORITY-AUTONOMOUS-
    # RELEASE-20260813.md): materialization is allowed either in the legacy
    # report-only posture or under the validated autonomous machine-gate
    # contract. Any half-flipped state stays forbidden.
    if not (legacy_report_only or autonomous):
        raise RuntimeError("automatic_generation_or_publication_forbidden")
    resolution = config["_current_content_base_resolution"]
    if Path(resolution["state"]["path"]) != BASE_STATE.resolve():
        raise RuntimeError("current_content_base_state_path_mismatch")
    verify_resolution(resolution)
    release_root = validate_unused_release_root(
        Path(args.release_root),
        Path(resolution["pointer"]["release_root"]),
        Path(resolution["flagship_root"]),
    )
    receipt, selected = load_selection_receipt(args.selection_receipt.resolve())
    if receipt.get("operation", "new_article") != args.operation:
        raise RuntimeError("selection_operation_mismatch")
    if selected["topic_id"] != args.topic_id:
        raise RuntimeError("selection_topic_id_mismatch")
    if selected["target_route"] != args.route:
        raise RuntimeError("selection_route_mismatch")
    r2_authority_path: Path | None = None
    validated_r2_authority: dict[str, Any] | None = None
    r2_subject: dict[str, Any] | None = None
    if args.operation == "r2_refresh":
        update_spec = (receipt.get("inputs") or {}).get("update_authority")
        if not (
            isinstance(update_spec, dict)
            and isinstance(update_spec.get("path"), str)
            and isinstance(update_spec.get("sha256"), str)
            and len(update_spec["sha256"]) == 64
        ):
            raise RuntimeError("selection_update_authority_spec_invalid")
        r2_authority_path = args.r2_authority.resolve(strict=True)
        selected_authority_path = Path(update_spec["path"]).resolve(strict=True)
        if r2_authority_path != selected_authority_path:
            raise RuntimeError("r2_selection_authority_path_mismatch")
        authority_digest = sha(r2_authority_path)
        if authority_digest != update_spec["sha256"]:
            raise RuntimeError("selection_update_authority_hash_invalid")
        validated_r2_authority = validate_update_authority(
            r2_authority_path,
            config_path=args.config.resolve(strict=True),
        )
        if sha(r2_authority_path) != authority_digest:
            raise RuntimeError("r2_authority_changed_during_validation")
        r2_subject = r2_materialization_subject(validated_r2_authority)
        expected_subject = {
            "topic_id": args.topic_id,
            "article_id": selected.get("article_id"),
            "target_query": selected.get("target_query"),
            "target_route": args.route,
            "r2_contract": selected.get("r2_contract"),
        }
        for key, value in expected_subject.items():
            if r2_subject.get(key) != value:
                raise RuntimeError(f"r2_selection_authority_field_mismatch:{key}")
    receipt, selected, receipt_spec = bind_packet_selection(packet, args.selection_receipt.resolve())
    queue_key = "update_authority" if args.operation == "r2_refresh" else "topic_queue"
    if args.operation == "r2_refresh":
        if r2_authority_path is None or validated_r2_authority is None or r2_subject is None:
            raise RuntimeError("r2_authority_required")
        queue_path = r2_authority_path
    else:
        queue_path = (
            args.topic_authority_snapshot.resolve(strict=True)
            if args.topic_authority_snapshot is not None
            else Path(receipt["inputs"][queue_key]["path"])
        )
    if sha(queue_path) != receipt["inputs"][queue_key]["sha256"]:
        raise RuntimeError(f"selection_{queue_key}_hash_invalid")
    queue = validated_r2_authority if validated_r2_authority is not None else json.loads(queue_path.read_text())
    queue_snapshot = Path(args.packet).resolve().parent / f"topic-queue-snapshot-{args.run_id}.json"
    queue_snapshot.write_text(queue_path.read_text())
    if args.operation == "r2_refresh":
        topic = r2_subject
        directive = {}
    else:
        topic = next((row for row in queue.get("queue") or [] if row.get("topic_id") == args.topic_id), None)
        directive = (topic or {}).get("competitor_gap") or {}
        if not (topic and topic.get("evidence_state") == "READY_FOR_RESEARCH"
                and directive.get("operator_ready_for_research") is True and directive.get("operator_directive_id")):
            raise RuntimeError("topic_go_not_proven")
        if topic["target_query"] != selected["target_query"]:
            raise RuntimeError("selection_query_mismatch")
        if directive.get("target_route") != selected["target_route"]:
            raise RuntimeError("selection_competitor_route_mismatch")
    authority_path = packet / "00-authority.json"
    if not authority_path.is_file():
        raise RuntimeError("packet_authority_missing")
    validate_packet_authority(json.loads(authority_path.read_text()), args.topic_id,
                              topic["target_query"], args.route)
    validator = SYSTEM / "scripts/validate_opus_article_packet.py"
    compiler = SYSTEM / "scripts/compile_review_packet_article.py"
    validator_argv: list[Path | str] = [
        PYTHON, validator, "--packet", packet,
        "--expected-topic-id", args.topic_id, "--expected-query", selected["target_query"],
        "--expected-route", args.route, "--selection-receipt", args.selection_receipt.resolve(),
        "--operation", args.operation, "--config", args.config.resolve(strict=True),
    ]
    if r2_authority_path is not None:
        validator_argv.extend(["--r2-authority", r2_authority_path])
    result = subprocess.run([str(item) for item in validator_argv],
                            text=True, capture_output=True, timeout=300)
    if result.returncode:
        repair_outcome = run_bounded_qa_repair(
            packet=packet,
            validator_argv=[str(item) for item in validator_argv],
            repair_argv=[str(PYTHON), str(SYSTEM / "scripts/repair_opus_packet.py")],
            history_dir=run_root / f"qa-repair-{args.run_id}",
            job_id=args.run_id,
        )
        if repair_outcome.get("all_checks_passed") is not True:
            raise RuntimeError(
                "deterministic_packet_qa_repair_exhausted:"
                f"{repair_outcome.get('closeout_path')}:"
                f"{repair_outcome.get('final_failures')}"
            )
    article = json.loads((packet / "06-article.json").read_text())
    if article.get("canonical_path") != args.route or (
        args.operation == "new_article" and args.topic_id not in article.get("article_id", "")
    ):
        raise RuntimeError("article_topic_or_route_mismatch")
    if args.operation == "r2_refresh" and article.get("article_id") != selected.get("article_id"):
        raise RuntimeError("r2_article_id_mismatch")
    build = SYSTEM / "scripts/build_opus_article_release.py"
    publish = SYSTEM / "scripts/publish_opus_article_release.py"
    engine = SYSTEM / "scripts/deploy_opus_article_engine.py"
    apply = SYSTEM / "scripts/apply_opus_article_release.py"
    repair = SYSTEM / "scripts/repair_opus_packet.py"
    review = SYSTEM / "scripts/opus_review_loop.py"
    schema = SYSTEM / "schemas/glp-opus-seo-review-v1.schema.json"
    active_transport = review_transports.transport(args.review_transport)
    wrapper = Path(active_transport["wrapper"])
    sol_wrapper = BUSINESS / "scripts/hermes_sol_article_review.py"
    opus_wrapper = BUSINESS / "scripts/claude_opus_article_review.py"
    transports_module = SYSTEM / "scripts/review_transports.py"
    tree_materializer = BUSINESS / "scripts/site_tree_materializer.py"
    rubric = BUSINESS / "reports/glp-clinic-scout/seo-machine/OPUS-REVIEW-PATH-20260802.md"
    gsc = BUSINESS / "systems/seo-blog-machine/scripts/gsc_direct_principal_controller.py"
    verifier = SYSTEM / "scripts/verify_opus_article_production.py"
    indexnow = SYSTEM / "scripts/submit_target_indexnow_once.py"
    bing_submitter = SYSTEM / "scripts/submit_target_bing_once.py"
    purge = BUSINESS / "scripts/recover_glp_wave2_cache_purge_20260729.py"
    controls = [build, tree_materializer, publish, engine, apply, compiler, validator, review, repair,
                SYSTEM / "scripts/deterministic_qa_repair.py", SYSTEM / "scripts/article_research.py",
                SYSTEM / "scripts/weekly_strategy_consumer.py",
                SYSTEM / "scripts/r2_update_carrier.py", schema,
                sol_wrapper, opus_wrapper, transports_module,
                gsc, verifier, indexnow, bing_submitter, purge, rubric, SYSTEM / "config/verticals/glp-clinic-scout.json",
                # Selection and prioritization are complete before materialization and
                # remain bound by the immutable selection receipt/snapshot below. The
                # selector is deliberately not an apply control: changing priority for
                # the next run must not strand an independently reviewed current run.
                SYSTEM / "pipeline-contract.json",
                SYSTEM / "scripts/materialize_opus_run.py", SYSTEM / "scripts/selection_receipt.py",
                SYSTEM / "scripts/current_content_base.py", SYSTEM / "scripts/carrier_lock.py",
                SYSTEM / "scripts/article_buffer_cli.py",
                # existing_article_updates.py also owns live prioritization and
                # may change while this packet is under independent review. Pin
                # its exact R2 build surface separately, not unrelated ranking.
                SYSTEM / "scripts/r2_execution_control.py",
                SYSTEM / "scripts/article_images.py",
                SYSTEM / "scripts/gpt_image_2_native.py"]
    inbound_plan = article.get("inbound_link_plan") or []
    section_index_changed = existing_section_index_changes(
        Path(resolution["pointer"]["release_root"]), route_parts
    )
    visual_key = route_parts["visual_key"]
    visual_added = [f"assets/images/{visual_key}{suffix}" for suffix in
                    ["-800.avif", "-1600.avif", "-800.webp", "-1600.webp", "-800.jpg", "-1600.jpg", "-og.jpg"]]
    report_dir = run_root / f"publication-{args.run_id}"
    materialized_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    materialized_pt_date = args.pt_date or dt.datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    bound_selection = selection_binding(receipt)
    # Close the discovery/write race immediately before minting apply controls.
    validate_unused_release_root(
        release_root,
        Path(resolution["pointer"]["release_root"]),
        Path(resolution["flagship_root"]),
    )
    # Both release modes must derive inbound-page authority from the CURRENT
    # accepted catalogs.  A reviewed buffer packet may already have its planned
    # link because an intervening release added it.  Declaring that page changed
    # anyway makes restore_linked_subtree require an output the build correctly
    # left untouched (the 2026-08-31 repeated restore_declared_output_missing
    # carrier failure).  Keep the packet plan immutable; narrow only the public
    # delta to work still unsatisfied in accepted truth.
    current_articles = [
        row
        for catalog in config["paths"]["content_catalogs"]
        for row in json.loads(Path(catalog["_resolved_path"]).read_text()).get(
            catalog["records_key"]
        ) or []
        if row.get("canonical_path")
    ]
    if args.operation == "r2_refresh":
        update_ledger = args.update_event_ledger.resolve(strict=True)
        update_ledger_sha = sha(update_ledger)
        current_routes = {
            str(row.get("canonical_path"))
            for row in current_articles
        }
        existing = [
            row
            for row in current_articles
            if row.get("canonical_path") == args.route and row.get("article_id") == article.get("article_id")
        ]
        if len(existing) != 1:
            raise RuntimeError(f"r2_current_article_exact_match_invalid:{len(existing)}")
        inbound_changed = unsatisfied_inbound_public_changes(current_articles, inbound_plan)
        r2_contract = validate_r2_execution_contract(
            {**(article.get("r2_contract") or {}), "current_routes": sorted(current_routes)},
            route=args.route,
        )
        allowed_added: list[str] = []
        # A same-route replacement does not change a profile's route-only
        # authored-pages list when the byline is stable. If the byline changes,
        # both predecessor and successor profiles are exact public deltas.
        author_profile_changes = author_profile_public_changes(existing[0], article)
        related_card_changes = related_article_card_public_paths(
            Path(resolution["pointer"]["release_root"]),
            existing[0],
            article,
        )
        allowed_changed = sorted({
            route_parts["target_public_rel"],
            *section_index_changed,
            *author_profile_changes,
            "sitemap.xml",
            *inbound_changed,
            *related_card_changes,
        })
    else:
        update_ledger = None
        update_ledger_sha = None
        r2_contract = None
        inbound_changed = unsatisfied_inbound_public_changes(
            current_articles,
            inbound_plan,
        )
        insights_delta = new_article_insights_public_delta(
            Path(resolution["pointer"]["release_root"]),
            args.route,
        )
        allowed_added = sorted({
            route_parts["target_public_rel"],
            *visual_added,
            *insights_delta["added"],
        })
        # A new catalog article is also added to the organizational author's
        # rendered authored-pages list. This is a real final public delta, not a
        # transient build change: omitting it made the route-scoped lastmod gate
        # abort every article build as stale before the validator suite ran.
        allowed_changed = sorted({
            *section_index_changed,
            author_profile_public_path(article),
            "index.html",
            "sitemap.xml",
            *inbound_changed,
            *insights_delta["changed"],
        })
    apply_template = {
        "schema": "glp-opus-article-apply-template-v1", "manifest_id": args.run_id,
        "operation": args.operation,
        "topic_id": args.topic_id, "target_query": topic["target_query"],
        "article_id": article["article_id"], "target_route": args.route,
        "selection_receipt": receipt_spec, "selection_binding": bound_selection,
        "materialization_reason": args.materialization_reason,
        "metadata_title": article["title"],
        "publication_date": args.publication_date, "article_json_path": str(packet / "06-article.json"),
        "operator_authority_path": str(queue_path), "base_state_path": str(BASE_STATE),
        "base_state_sha256": sha(BASE_STATE), "published_ledger_path": str(PUBLISHED_LEDGER),
        "published_ledger_sha256": sha(PUBLISHED_LEDGER),
        "update_event_ledger_path": str(update_ledger) if update_ledger else None,
        "update_event_ledger_sha256": update_ledger_sha,
        "release_root": str(release_root),
        "report_dir": str(report_dir), "allowed_public_added": allowed_added,
        "allowed_public_changed": allowed_changed,
        "inbound_link_plan": inbound_plan,
        "r2_contract": r2_contract,
        "r2_execution_surface": (
            surface_spec(SYSTEM / "scripts/existing_article_updates.py")
            if args.operation == "r2_refresh" else None
        ),
        "control_files": {str(path.resolve()): sha(path) for path in controls},
        "proven_deployer_path": str(engine), "builder_path": str(build), "publisher_path": str(publish),
    }
    apply_path = run_root / f"apply-template-{args.run_id}.json"; atomic(apply_path, apply_template)
    job = {
        "schema": "glp-opus-seo-review-job-v1", "job_id": args.run_id, "topic_id": args.topic_id,
        "target_query": selected["target_query"], "target_route": selected["target_route"],
        "selection_receipt": receipt_spec, "selection_binding": bound_selection,
        "materialized_at_utc": materialized_at, "materialized_pt_date": materialized_pt_date,
        "artifact_role": "new_article_review_job" if args.operation == "new_article" else "r2_refresh_review_job",
        "operation": args.operation, "materialization_reason": args.materialization_reason,
        "packet_dir": str(packet), "packet_authority_file": "00-authority.json",
        "history_dir": str(run_root / f"opus-history-{args.run_id}"), "max_rounds": 5,
        # Pin a run-scoped SNAPSHOT of the queue, not the live file: the queue
        # legitimately regenerates (weekly learning, daily feed) and pinning
        # latest.json stranded two in-flight reviews on 2026-08-06 with
        # topic_authority_hash_invalid. The snapshot freezes exactly the bytes
        # the selector verified; eligibility was already proven at selection.
        "review_timeout": 1200, "topic_authority": spec(queue_snapshot),
        "review_transport": active_transport["transport_id"],
        "subject_files": ["00-authority.json", "01-evidence-ledger.json", "02-serp-brief.json",
                          "03-editorial-brief.md", "04-draft.md", "05-claim-source-map.json",
                          "06-article.json", "07-qa-receipt.json"],
        "review_wrapper_sha256": sha(wrapper), "rubric_sha256": sha(rubric), "review_schema_sha256": sha(schema),
        "prompt_intro": f"Review the exact {selected['target_query']} U.S. YMYL packet against the five-gate rubric. Require source binding, medical safety, SERP parity, verified-organization E-E-A-T, cluster links, mobile tables, Article+FAQ schema inputs, and the declared delivery contract (explicit ON_PAGE_ONLY guides require useful on-page content and zero email promise/forms/fields; email-delivery variants retain the consented capture and owner-record privacy contract).",
        "validator": executable(
            validator_argv,
            [validator, compiler, SYSTEM / "scripts/selection_receipt.py",
             SYSTEM / "scripts/r2_update_carrier.py"],
            300,
        ),
        "repair": executable([PYTHON, repair], [repair], REPAIR_CONTROLLER_TIMEOUT_SECONDS,
                             derived_files=["06-article.json", "07-qa-receipt.json"]),
        "publish": executable([PYTHON, apply, "--template", apply_path], [apply, apply_path], 3600),
    }
    review_path = run_root / f"review-job-{args.run_id}.json"; atomic(review_path, job)
    print(json.dumps({"status": "PASS_OPUS_RUN_MATERIALIZED", "review_job": str(review_path),
                      "review_job_sha256": sha(review_path), "apply_template": str(apply_path),
                      "apply_template_sha256": sha(apply_path), "qa": json.loads((packet / "07-qa-receipt.json").read_text())["status"]}, sort_keys=True))


if __name__ == "__main__":
    main()
