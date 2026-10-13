"""GLP-320 five-gate repair: the native compiler keeps H3 semantics and table
positions and declares the accepted-guide render contract (TOC, emphasis,
source-ID citations, no generated three-column recap)."""
from test_publication_date_binding import module, packet


def test_h3_subheadings_are_semantic_not_prose(tmp_path):
    compiler = module("compile_review_packet_article")
    body = ("## Parent\n\nIntro paragraph. [S1]\n\n| A | B |\n|---|---|\n| x | y |\n\n"
            "After table.\n\n### Sub one\n\nUnder sub one. **Bold** [S1]\n\n### Sub two\n\nUnder sub two.\n")
    sections, faqs = compiler.parse_sections(body)
    assert len(sections) == 1 and faqs == []
    section = sections[0]
    assert section["paragraphs"] == ["Intro paragraph. [S1]", "After table.",
                                      "Under sub one. **Bold** [S1]", "Under sub two."]
    assert section["subheadings"] == [{"heading": "Sub one", "before_paragraph": 2, "order": 3},
                                      {"heading": "Sub two", "before_paragraph": 3, "order": 5}]
    assert all(not p.rstrip(".").endswith(("Sub one", "Sub two")) for p in section["paragraphs"])
    assert section["tables"] == [{"headers": ["A", "B"], "rows": [["x", "y"]], "after_paragraph": 1, "order": 1}]


def test_compiled_article_declares_render_contract_and_keeps_word_count(tmp_path):
    compiler = module("compile_review_packet_article")
    packet(tmp_path)
    article = compiler.compile_packet(tmp_path)
    assert article["render_contract"] == {
        "schema": "glp-accepted-guide-render-v1", "subheadings": "h3", "toc": "automatic-sections",
        "inline_emphasis": True, "citations": "source-id", "facts_recap": False}
    body = (tmp_path / "04-draft.md").read_text().split("---", 2)[2].strip()
    assert article["word_count"] == compiler.words(body)


def _blocks(section):
    rows = [("table", t["after_paragraph"], t["order"]) for t in section.get("tables") or []]
    rows += [("h3", h["before_paragraph"], h["order"]) for h in section.get("subheadings") or []]
    return sorted(rows, key=lambda row: row[2])


def test_cr05_table_and_h3_at_same_paragraph_offset_keep_authored_order():
    """CR-05: offsets alone tie; the shared ``order`` key keeps authored order.
    The site renderer test (tests/accepted-guide-render-contract.test.mjs CR-05)
    renders exactly these two block shapes."""
    compiler = module("compile_review_packet_article")
    table = "| A | B |\n|---|---|\n| x | y |\n"
    before, _ = compiler.parse_sections("## Parent\n\nIntro. [S1]\n\n" + table + "\n### Sub\n\nSecond. [S1]\n")
    assert _blocks(before[0]) == [("table", 1, 1), ("h3", 1, 2)]
    after, _ = compiler.parse_sections("## Parent\n\nIntro. [S1]\n\n### Sub\n\n" + table + "\nSecond. [S1]\n")
    assert _blocks(after[0]) == [("h3", 1, 1), ("table", 1, 2)]
    # A table followed immediately by prose (no blank line) still precedes it.
    tight, _ = compiler.parse_sections("## Parent\n\nIntro.\n\n" + table + "Second.\n")
    assert tight[0]["paragraphs"] == ["Intro.", "Second."] and _blocks(tight[0]) == [("table", 1, 1)]
    for section in (before[0], after[0]):
        orders = [row[2] for row in _blocks(section)]
        assert len(set(orders)) == len(orders)
        assert all(order == at + index for index, (_, at, order) in enumerate(_blocks(section)))
