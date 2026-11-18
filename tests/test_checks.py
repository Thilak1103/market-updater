from market_updater.analysis.checks import check_section, link_citations, numbers_in


def test_index_names_are_not_numbers():
    assert numbers_in("The FTSE 100 and S&P 500 rose; 7203.T fell.") == []


def test_unsourced_number_sentence_is_dropped():
    claims = {1: {"claim": "Margins rose to 4.2%.", "evidence": "", "numbers": ["4.2%"], "url": "https://x.com/a"}}
    md = "Margins rose to 4.2% [c1]. Sales jumped 17% overnight. Volumes fell 3.1% [m]."
    clean, problems = check_section(md, claims, {"3.1%"})
    assert "4.2%" in clean and "3.1%" in clean
    assert "17%" not in clean
    assert any("17%" in p for p in problems)


def test_unknown_citation_removed_and_links_made():
    claims = {1: {"claim": "x", "url": "https://example.com/p"}}
    clean, problems = check_section("A view [c1]. Another [c42].", claims, set())
    assert "[c42]" not in clean and any("c42" in p for p in problems)
    assert link_citations(clean, claims).count("](https://example.com/p)") == 1


def test_tables_and_headings_pass_through():
    md = "## Heading 2026\n| A | 55.5% |\n|---|---|"
    clean, problems = check_section(md, {}, set())
    assert clean == md and not problems


def test_decimals_are_not_mistaken_for_tickers():
    assert numbers_in("Shares rose 12.34% and EXV1.DE fell; 1211.HK flat.") == ["12.34%"]


def test_draft_number_cannot_be_rounded_to_match():
    clean, _ = check_section("It rose 12.34% today.", {}, {"12%"})
    assert clean == ""
    clean, _ = check_section("It rose 4.1% today.", {}, {"4.12%"})
    assert "4.1%" in clean
