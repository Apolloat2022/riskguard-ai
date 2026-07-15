# RiskGuard AI — Showcase Summary

A short, shareable description of this project (e.g. for LinkedIn). See `PLAN.md` for the
full architectural blueprint and `README.md` for setup/usage.

---

Built RiskGuard AI — an end-to-end risk analytics & automated remediation platform
combining traditional ML with agentic AI.

🔹 **ML pipeline**: XGBoost model trained on imbalanced loan data (imputation, scaling,
class-imbalance handling via `scale_pos_weight`/SMOTE, threshold tuning) — 0.90 ROC-AUC
🔹 **Backend**: Async FastAPI + SQLAlchemy 2.0 over Postgres (Neon), with a
hand-optimized CTE for real-time feature assembly
🔹 **RAG**: A parent-child chunking retriever over state-specific lending policy
documents
🔹 **Agentic AI**: A stateful LangGraph workflow — powered by Claude (Anthropic) on
Amazon Bedrock — that retrieves policy context, drafts a compliant remediation plan,
runs it through automated compliance checks with a bounded revision loop, and pauses for
human sign-off via a human-in-the-loop `interrupt()` before finalizing
🔹 **MCP server**: A Model Context Protocol layer (`FastMCP`, streamable-HTTP transport)
exposing the same risk/remediation workflow as first-class tools an external AI client —
Claude Desktop, Cursor, or any MCP-speaking agent — can call directly, with zero
duplicated business logic: `assess_loan_risk`, `get_remediation_case`, and
`resume_remediation_case` are thin adapters over the existing REST endpoints, plus a
`compliance://regulations/{state}` resource serving the same policy corpus the RAG
retriever draws from

The result: when a customer crosses a default-risk threshold, the system doesn't just
flag it — it drafts a compliant, policy-grounded remediation plan and routes it to a
human for approval, with a full audit trail at every step. And it's not locked behind a
custom UI: any MCP-compatible agent can assess risk, pull a remediation case, or approve
one, the same way a human operator would through the REST API.

`#MachineLearning #GenAI #AgenticAI #LangGraph #FastAPI #Claude #MLOps #MCP #ModelContextProtocol`
