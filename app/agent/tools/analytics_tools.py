import json
from datetime import date, datetime, timezone

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import APIError
from app.database.connection import SessionLocal
from app.schemas.analytics import date_range_error
from app.schemas.intelligence import InsightCategory
from app.services import analytics_service, proactive_insights_service

from .common import error_output, success_output
from .context import AgentToolContext


class SalesAnalyticsInput(BaseModel):
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def date_range_is_valid(self) -> "SalesAnalyticsInput":
        if error := date_range_error(self.start_date, self.end_date):
            raise ValueError(error)
        return self


class ProactiveInsightsInput(BaseModel):
    category: InsightCategory | None = Field(
        default=None,
        description="overdue_followup, stalled_deal, neglected_lead or needs_attention; "
        "omit for all.",
    )
    customer_id: int | None = Field(
        default=None, ge=1, description="Only this customer's recommendations."
    )
    limit: int = Field(
        default=10, ge=1, le=20, description="Highest-priority recommendations to return."
    )


# Fields the model sees; scores and evidence explain each item, links let the user open it.
_INSIGHT_FIELDS = (
    "recommendation_id", "category", "title", "reason", "urgency", "potential_impact",
    "priority_score", "evidence", "recommended_next_action", "customer_id", "customer_name",
    "enquiry_id", "followup_id", "meeting_id", "links",
)


def build_analytics_tools(
    db: Session | None = None,
    context: AgentToolContext | None = None,
    *,
    session_factory: sessionmaker[Session] | None = None,
) -> list[StructuredTool]:

    if context is None:
        raise ValueError("CRM tools require a tenant context.")
    organization_id = context.organization_id

    factory = session_factory

    def get_session() -> tuple[Session, bool]:
        if factory is not None:
            return factory(), True
        if db is not None:
            return db, False
        return SessionLocal(), True

    def get_sales_analytics(
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> str:
        """Retrieve deterministic, pre-calculated CRM overview, pipeline, sales lead (qualification, won/lost outcome, win rate), activity and follow-up analytics."""

        session, owns_session = get_session()

        try:
            results = {
                "overview": analytics_service.get_overview_metrics(
                    session,
                    start_date,
                    end_date,
                    organization_id=organization_id,
                ).model_dump(mode="json"),
                "customers": analytics_service.get_customer_metrics(
                    session,
                    start_date,
                    end_date,
                    organization_id=organization_id,
                ).model_dump(mode="json"),
                "enquiries": analytics_service.get_enquiry_metrics(
                    session,
                    start_date,
                    end_date,
                    organization_id=organization_id,
                ).model_dump(mode="json"),
                "activities": analytics_service.get_activity_metrics(
                    session,
                    start_date,
                    end_date,
                    organization_id=organization_id,
                ).model_dump(mode="json"),
                "pipeline": analytics_service.get_pipeline_metrics(
                    session,
                    start_date,
                    end_date,
                    organization_id=organization_id,
                ).model_dump(mode="json"),
                "followups": analytics_service.get_followup_metrics(
                    session,
                    start_date,
                    end_date,
                    organization_id=organization_id,
                ).model_dump(mode="json"),
            }

            return json.dumps(
                results,
                ensure_ascii=False,
            )

        finally:
            if owns_session:
                session.close()

    def get_proactive_insights(
        category: InsightCategory | None = None,
        customer_id: int | None = None,
        limit: int = 10,
    ) -> str:
        """Ranked, rule-based recommendations with evidence and record links. Read-only.

        Covers overdue follow-ups, neglected leads, stalled deals and customers needing attention.
        """
        session, owns_session = get_session()
        try:
            if context.locked_customer_id is not None:
                context.ensure_customer_access(customer_id or context.locked_customer_id)
                customer_id = context.locked_customer_id
            result = proactive_insights_service.get_proactive_insights(
                session,
                organization_id=organization_id,
                now=datetime.now(timezone.utc),
                category=category,
                customer_id=customer_id,
                page_size=limit,
            )
            items = [
                item.model_dump(mode="json", include=set(_INSIGHT_FIELDS)) for item in result.items
            ]
            for item in items:
                # Identified through this scoped read, so action proposals may reference them.
                context.remember_customer(item["customer_id"])
                if item["enquiry_id"] is not None:
                    context.remember_enquiry(item["customer_id"], item["enquiry_id"])
                for link in item["links"]:
                    if link["record_type"] == "followup":
                        context.remember_followup(item["customer_id"], link["record_id"])
                    elif link["record_type"] == "meeting":
                        context.remember_meeting(item["customer_id"], link["record_id"])
            return success_output(
                {
                    "items": items,
                    "returned": len(items),
                    "total_matching": result.total,
                    "counts_by_category": result.counts_by_category,
                    **({} if items else {"note": "No recommendations match these rules."}),
                }
            )
        except APIError as exc:
            return error_output(exc)
        finally:
            if owns_session:
                session.close()

    return [
        StructuredTool.from_function(
            func=get_proactive_insights,
            name="get_proactive_insights",
            description=get_proactive_insights.__doc__,
            args_schema=ProactiveInsightsInput,
        ),
        StructuredTool.from_function(
            func=get_sales_analytics,
            name="get_sales_analytics",
            description=get_sales_analytics.__doc__,
            args_schema=SalesAnalyticsInput,
        )
    ]