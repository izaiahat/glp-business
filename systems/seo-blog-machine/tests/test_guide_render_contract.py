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
    assert section["subheadings"] == [{"heading": "Sub one", "before_paragraph": 2},
                                      {"heading": "Sub two", "before_paragraph": 3}]
    assert all(not p.rstrip(".").endswith(("Sub one", "Sub two")) for p in section["paragraphs"])
    assert section["tables"] == [{"headers": ["A", "B"], "rows": [["x", "y"]], "after_paragraph": 1}]


def test_compiled_article_declares_render_contract_and_keeps_word_count(tmp_path):
    compiler = module("compile_review_packet_article")
    packet(tmp_path)
    article = compiler.compile_packet(tmp_path)
    assert article["render_contract"] == {
        "schema": "glp-accepted-guide-render-v1", "subheadings": "h3", "toc": "automatic-sections",
        "inline_emphasis": True, "citations": "source-id", "facts_recap": False}
    body = (tmp_path / "04-draft.md").read_text().split("---", 2)[2].strip()
    assert article["word_count"] == compiler.words(body)
