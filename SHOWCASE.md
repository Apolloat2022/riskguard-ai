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

The result: when a customer crosses a default-risk threshold, the system doesn't just
flag it — it drafts a compliant, policy-grounded remediation plan and routes it to a
human for approval, with a full audit trail at every step.

`#MachineLearning #GenAI #AgenticAI #LangGraph #FastAPI #Claude #MLOps`
