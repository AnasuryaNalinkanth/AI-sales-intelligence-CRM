from datetime import datetime, timezone
from typing import Any, TypeGuard

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.exceptions import APIError
from app.database.connection import rollback_failed_transaction
from app.models import HitlPolicyAudit, HitlPolicyOverride, User
from app.schemas.agent import AgentActionMode, AgentActionName

VALID_MODES: frozenset[AgentActionMode] = frozenset(
    {"automatic", "approval_required", "disabled"}
)

DEFAULT_POLICIES: dict[AgentActionName, AgentActionMode] = {
    "create_meeting": "approval_required",
    "create_followup": "approval_required",
    "schedule_call": "approval_required",
    "record_call_result": "approval_required",
    "complete_followup": "approval_required",
    "apply_enrichment": "approval_required",
    "update_lead_qualification": "approval_required",
    "close_opportunity": "approval_required",
}
# Qualification decisions and deal closures always need a human: never automatic.
APPROVAL_ONLY_ACTIONS: frozenset[AgentActionName] = frozenset(
    {"update_lead_qualification", "close_opportunity"}
)


def _is_policy_action(value: str) -> TypeGuard[AgentActionName]:
    return value in DEFAULT_POLICIES


def _is_policy_mode(value: str) -> TypeGuard[AgentActionMode]:
    return value in VALID_MODES


# Overrides belong to one organization; DEFAULT_POLICIES apply wherever none is set.
class ActionPolicyStore:
    def list_policies(
        self, db: Session, *, organization_id: int
    ) -> dict[AgentActionName, AgentActionMode]:
        policies = dict(DEFAULT_POLICIES)
        try:
            overrides = db.scalars(
                select(HitlPolicyOverride).where(
                    HitlPolicyOverride.organization_id == organization_id
                )
            ).all()
        except SQLAlchemyError as exc:
            rollback_failed_transaction(db)
            raise APIError(
                "The action policies could not be loaded.",
                500,
                "hitl_persistence_error",
            ) from exc

        for override in overrides:
            if _is_policy_action(override.action_type):
                if not _is_policy_mode(override.mode):
                    raise APIError(
                        "A persisted action policy has an unsupported mode.",
                        500,
                        "invalid_persisted_policy",
                    )
                policies[override.action_type] = (
                    "approval_required"
                    if override.mode == "automatic"
                    and override.action_type in APPROVAL_ONLY_ACTIONS
                    else override.mode
                )
        return policies

    def get_policy(
        self,
        db: Session,
        action: AgentActionName,
        *,
        organization_id: int,
        for_update: bool = False,
    ) -> AgentActionMode:
        try:
            statement = select(HitlPolicyOverride).where(
                HitlPolicyOverride.organization_id == organization_id,
                HitlPolicyOverride.action_type == action,
            )
            if for_update:
                statement = statement.with_for_update()
            override = db.scalar(statement)
        except SQLAlchemyError as exc:
            rollback_failed_transaction(db)
            raise APIError(
                "The action policy could not be loaded.",
                500,
                "hitl_persistence_error",
            ) from exc
        if override is None:
            return DEFAULT_POLICIES[action]
        if not _is_policy_mode(override.mode):
            raise APIError(
                "The persisted action policy has an unsupported mode.",
                500,
                "invalid_persisted_policy",
            )
        if override.mode == "automatic" and action in APPROVAL_ONLY_ACTIONS:
            return "approval_required"
        return override.mode

    def set_policy(
        self,
        db: Session,
        action: AgentActionName,
        mode: AgentActionMode,
        *,
        organization_id: int,
        changed_by: str,
    ) -> tuple[AgentActionMode, AgentActionMode, datetime]:
        if action not in DEFAULT_POLICIES:
            raise APIError(
                "Unsupported action type for policy configuration.",
                422,
                "unsupported_action_policy",
            )
        if mode not in VALID_MODES:
            raise APIError(
                "The selected policy mode is not supported.",
                422,
                "invalid_policy_mode",
            )
        if mode == "automatic" and action in APPROVAL_ONLY_ACTIONS:
            raise APIError(
                "This action always requires approval and cannot run automatically.",
                422,
                "automatic_not_allowed",
            )

        try:
            actor_id = db.scalar(
                select(User.user_id).where(
                    User.email == changed_by,
                    User.organization_id == organization_id,
                )
            )
            if actor_id is None:
                raise APIError(
                    "The policy administrator could not be identified.",
                    403,
                    "forbidden",
                )

            override = db.scalar(
                select(HitlPolicyOverride)
                .where(
                    HitlPolicyOverride.organization_id == organization_id,
                    HitlPolicyOverride.action_type == action,
                )
                .with_for_update()
            )
            previous = (
                override.mode
                if override is not None
                else DEFAULT_POLICIES[action]
            )
            if not _is_policy_mode(previous):
                raise APIError(
                    "The persisted action policy has an unsupported mode.",
                    500,
                    "invalid_persisted_policy",
                )
            changed_at = datetime.now(timezone.utc)
            if previous != mode:
                if override is None:
                    override = HitlPolicyOverride(
                        organization_id=organization_id,
                        action_type=action,
                        mode=mode,
                        updated_by_user_id=actor_id,
                        updated_at=changed_at,
                    )
                    db.add(override)
                else:
                    override.mode = mode
                    override.updated_by_user_id = actor_id
                    override.updated_at = changed_at

                db.add(
                    HitlPolicyAudit(
                        organization_id=organization_id,
                        action_type=action,
                        previous_mode=previous,
                        new_mode=mode,
                        changed_by_user_id=actor_id,
                        changed_at=changed_at,
                    )
                )
            db.commit()
            return previous, mode, changed_at
        except APIError:
            db.rollback()
            raise
        except SQLAlchemyError as exc:
            rollback_failed_transaction(db)
            raise APIError(
                "The action policy could not be saved.",
                500,
                "hitl_persistence_error",
            ) from exc

    def enforce_action_policy(
        self, db: Session, action: AgentActionName, *, organization_id: int
    ) -> None:
        mode = self.get_policy(db, action, organization_id=organization_id)
        if mode == "disabled":
            raise APIError(
                "This action is disabled by the global autonomy policy and cannot execute.",
                403,
                "action_disabled",
            )


policy_store = ActionPolicyStore()


def list_action_policies(
    db: Session, *, organization_id: int
) -> dict[AgentActionName, AgentActionMode]:
    return policy_store.list_policies(db, organization_id=organization_id)


def get_action_policy(
    db: Session,
    action: AgentActionName,
    *,
    organization_id: int,
    for_update: bool = False,
) -> AgentActionMode:
    return policy_store.get_policy(
        db, action, organization_id=organization_id, for_update=for_update
    )


def set_action_policy(
    db: Session,
    action: AgentActionName,
    mode: AgentActionMode,
    *,
    organization_id: int,
    changed_by: str,
) -> dict[str, Any]:
    previous, new_mode, changed_at = policy_store.set_policy(
        db,
        action,
        mode,
        organization_id=organization_id,
        changed_by=changed_by,
    )
    return {
        "action": action,
        "previous_mode": previous,
        "new_mode": new_mode,
        "changed_by": changed_by,
        "changed_at": changed_at.isoformat(),
    }


def enforce_action_policy(
    db: Session, action: AgentActionName, *, organization_id: int
) -> None:
    policy_store.enforce_action_policy(db, action, organization_id=organization_id)
