from app.rag.retriever import PolicyRetriever


def test_ca_rate_reduction_query_returns_ca_parent_chunk():
    retriever = PolicyRetriever()
    results = retriever.retrieve_policies("rate reduction California", "CA", k=3)

    assert results, "expected at least one parent chunk"
    assert any(p.source == "policy_CA.md" for p in results)
    # The CA-specific rate-reduction clause should be the top (or a top) hit,
    # not just present somewhere in the result set.
    assert results[0].source == "policy_CA.md"
    assert "rate reduction" in results[0].heading.lower()


def test_state_scoping_excludes_other_states():
    retriever = PolicyRetriever()
    results = retriever.retrieve_policies("rate reduction", "TX", k=5)

    sources = {p.source for p in results}
    assert "policy_TX.md" in sources
    assert "policy_CA.md" not in sources
    assert "policy_NY.md" not in sources


def test_unsupported_state_falls_back_to_general_policy_only():
    retriever = PolicyRetriever()
    results = retriever.retrieve_policies("forbearance limits", "FL", k=3)

    assert results
    assert all(p.source == "underwriting_general.md" for p in results)


def test_respects_k_limit():
    retriever = PolicyRetriever()
    results = retriever.retrieve_policies("compliance escalation disclosure requirements", "NY", k=2)

    assert len(results) <= 2


def test_stopword_only_query_returns_empty_without_error():
    retriever = PolicyRetriever()
    results = retriever.retrieve_policies("the a of", "CA", k=2)

    assert results == []
