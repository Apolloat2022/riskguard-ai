"""Graph wiring, checkpointer factory, and the run_case entry point that
drives one invocation (fresh trigger or human-decision resume) end to end.

Checkpointer lifecycle: the compiled graph (and its checkpointer instance)
is a process-wide singleton, built once via init_agent_graph() from the
FastAPI lifespan. This is load-bearing for MemorySaver — recompiling the
graph with a *new* MemorySaver() on every call would silently lose all
paused-case state, since MemorySaver only persists in that instance's memory.
Both the initial BackgroundTask trigger and the later /approve or /reject
request must resolve to the same compiled-graph object.

Per-node audit logging: rather than giving node functions DB access (which
would make app/agent/nodes.py untestable without a database), run_case
iterates the graph via `stream_mode="updates"` — LangGraph yields one
`{node_name: partial_state}` dict per completed node, and a `{"__interrupt__":
...}` entry when the graph pauses. See PLAN.md section 5.2's "every node
writes an audit_logs row" requirement.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.nodes import (
    compliance_check,
    escalate,
    finalize,
    human_review,
    plan_remediation,
    retrieve_context,
    route_compliance,
    route_human_decision,
)
from app.agent.state import AgentState
from app.config import settings
from app.db.models import AuditLog, Customer, RemediationCase, RiskAssessment

logger = logging.getLogger(__name__)


def _build_graph() -> StateGraph:
    graph = StateGraph(AgentState)
    graph.add_node("retrieve_context", retrieve_context)
    graph.add_node("plan_remediation", plan_remediation)
    graph.add_node("compliance_check", compliance_check)
    graph.add_node("human_review", human_review)
    graph.add_node("finalize", finalize)
    graph.add_node("escalate", escalate)

    graph.add_edge(START, "retrieve_context")
    graph.add_edge("retrieve_context", "plan_remediation")
    graph.add_edge("plan_remediation", "compliance_check")
    graph.add_conditional_edges(
        "compliance_check",
        route_compliance,
        {"passed": "human_review", "needs_revision": "plan_remediation", "rejected": "escalate"},
    )
    graph.add_conditional_edges(
        "human_review",
        route_human_decision,
        {"approved": "finalize", "denied": "plan_remediation"},
    )
    graph.add_edge("finalize", END)
    graph.add_edge("escalate", END)
    return graph


def _psycopg_dsn(database_url: str) -> str:
    """langgraph-checkpoint-postgres uses psycopg (libpq-style DSNs), not
    asyncpg — translate our SQLAlchemy `postgresql+asyncpg://...?ssl=require`
    into `postgresql://...?sslmode=require`."""
    url = make_url(database_url).set(drivername="postgresql")
    query = dict(url.query)
    if "ssl" in query:
        query["sslmode"] = query.pop("ssl")
    return url.set(query=query).render_as_string(hide_password=False)


@asynccontextmanager
async def _checkpointer_cm():
    if settings.checkpointer_backend == "postgres":
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(
            _psycopg_dsn(settings.database_url)
        ) as saver:
            await saver.setup()
            yield saver
    else:
        yield MemorySaver()


_graph_state: dict[str, object] = {"compiled": None, "exit_stack": None}


async def init_agent_graph() -> None:
    """Idempotent — safe to call more than once (e.g. across test cases)."""
    if _graph_state["compiled"] is not None:
        return
    stack = AsyncExitStack()
    checkpointer = await stack.enter_async_context(_checkpointer_cm())
    _graph_state["compiled"] = _build_graph().compile(checkpointer=checkpointer)
    _graph_state["exit_stack"] = stack


async def shutdown_agent_graph() -> None:
    stack = _graph_state.get("exit_stack")
    if stack is not None:
        await stack.aclose()
    _graph_state["compiled"] = None
    _graph_state["exit_stack"] = None


def get_compiled_graph():
    compiled = _graph_state["compiled"]
    if compiled is None:
        raise RuntimeError("agent graph not initialized — call init_agent_graph() first")
    return compiled


async def _build_initial_state(
    case_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> AgentState:
    async with session_factory() as session:
        case = await session.get(RemediationCase, uuid.UUID(case_id))
        if case is None:
            raise ValueError(f"remediation case {case_id} not found")
        assessment = await session.get(RiskAssessment, case.risk_assessment_id)
        customer = await session.get(Customer, case.customer_id)

    return AgentState(
        customer_id=customer.id,
        case_id=str(case.id),
        risk_score=float(assessment.default_probability),
        risk_features=assessment.features_snapshot,
        state_code=customer.state_code,
        retrieved_policies=[],
        generated_remediation_plan=None,
        compliance_status="pending",
        compliance_notes=None,
        human_approved=False,
        revision_count=0,
    )


async def run_case(
    case_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    resume: dict | None = None,
) -> None:
    """Drives one graph invocation for case_id: a fresh trigger (resume=None)
    or a resumption after a human decision (resume={"approved": bool, "notes":
    str}). Persists one audit_logs row per node executed plus the resulting
    status transition; any failure (LLM, retrieval, or otherwise) escalates
    the case rather than leaving it silently stuck."""
    config = {"configurable": {"thread_id": case_id}}
    graph = get_compiled_graph()
    stream_input = Command(resume=resume) if resume is not None else await _build_initial_state(
        case_id, session_factory
    )

    session = session_factory()
    try:
        last_node: str | None = None
        interrupted = False

        async for update in graph.astream(stream_input, config=config, stream_mode="updates"):
            if "__interrupt__" in update:
                interrupted = True
                break
            for node_name in update:
                last_node = node_name
                session.add(
                    AuditLog(
                        entity_type="remediation_case",
                        entity_id=case_id,
                        action=node_name.upper(),
                        actor="agent",
                        detail=None,
                    )
                )

        snapshot = await graph.aget_state(config)
        final_state = snapshot.values

        case = await session.get(RemediationCase, uuid.UUID(case_id))
        case.remediation_plan = final_state.get("generated_remediation_plan")
        case.compliance_notes = final_state.get("compliance_notes")
        case.revision_count = final_state.get("revision_count", case.revision_count)

        if interrupted:
            case.status = "AWAITING_HUMAN_REVIEW"
        elif last_node == "finalize":
            case.status = "APPROVED"
            case.resolved_at = datetime.now(timezone.utc)
        elif last_node == "escalate":
            case.status = "ESCALATED"
            case.resolved_at = datetime.now(timezone.utc)

        session.add(
            AuditLog(
                entity_type="remediation_case",
                entity_id=case_id,
                action=f"STATUS_{case.status}",
                actor="system",
                detail=None,
            )
        )
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logger.exception("remediation workflow failed for case %s", case_id)
        async with session_factory() as failure_session:
            case = await failure_session.get(RemediationCase, uuid.UUID(case_id))
            if case is not None:
                case.status = "ESCALATED"
                case.resolved_at = datetime.now(timezone.utc)
                case.compliance_notes = f"Workflow failed: {exc}"
                failure_session.add(
                    AuditLog(
                        entity_type="remediation_case",
                        entity_id=case_id,
                        action="WORKFLOW_FAILED",
                        actor="system",
                        detail={"error": str(exc)},
                    )
                )
                await failure_session.commit()
    finally:
        await session.close()
