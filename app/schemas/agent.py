from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.validators import validate_timezone_name

AgentActionMode = Literal["automatic", "approval_required", "disabled"]


class AgentChatRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    message: str = Field(min_length=1, max_length=4000)
    customer_id: int | None = Field(default=None, ge=1)
    timezone_name: str | None = None
    # Omitted to start a new conversation; otherwise one the server issued to this user.
    conversation_id: UUID | None = None

    @field_validator("timezone_name", mode="after")
    @classmethod
    def validate_user_timezone(cls, value: str | None) -> str | None:
        return validate_timezone_name(value) if value is not None else None


class AgentToolCall(BaseModel):
    name: str
    input_summary: dict[str, str | int]
    success: bool
    error_code: str | None = None


AgentActionName = Literal[
    "create_meeting",
    "create_followup",
    "schedule_call",
    "record_call_result",
    "complete_followup",
    "apply_enrichment",
    "update_lead_qualification",
    "close_opportunity",
]


class PendingAgentAction(BaseModel):
    action_id: str
    action: AgentActionName
    payload: dict[str, Any]


class AgentPolicyUpdateRequest(BaseModel):
    action: AgentActionName
    mode: AgentActionMode


class AgentActionResult(BaseModel):
    status: Literal["completed", "cancelled", "rejected", "failed"]
    action: AgentActionName
    record_id: int | None = None
    record: dict[str, Any] | None = None
    error_code: str | None = None
    message: str
    details: dict[str, Any] | None = None


class AgentTaskOperationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: AgentActionName
    values: dict[str, Any] = Field(default_factory=dict)


class AgentTaskStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operations: list[AgentTaskOperationInput] = Field(min_length=1, max_length=5)


class AgentTaskInputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_index: int = Field(ge=0, le=4)
    values: dict[str, Any]


class AgentTaskFormChoice(BaseModel):
    id: int | str
    label: str


class AgentTaskFormField(BaseModel):
    name: str
    label: str
    input_type: str
    required: bool
    value: Any = None
    help_text: str | None = None
    choices: list[AgentTaskFormChoice] = Field(default_factory=list)
    depends_on: str | None = None


class AgentTaskOperationResponse(BaseModel):
    action: AgentActionName
    status: str
    values: dict[str, Any]
    missing_fields: list[str] = Field(default_factory=list)
    fields: list[AgentTaskFormField] = Field(default_factory=list)
    action_id: str | None = None
    result: AgentActionResult | None = None


class AgentTaskResponse(BaseModel):
    task_id: str
    status: str
    expires_at: datetime
    operations: list[AgentTaskOperationResponse]
    message: str


class AgentChatResponse(BaseModel):
    response: str
    customer_id: int | None
    tool_calls: list[AgentToolCall]
    pending_action: PendingAgentAction | None = None
    clarification_task: AgentTaskResponse | None = None
    # Slots from the availability check that rejected the requested time.
    schedule_alternatives: list[dict[str, Any]] = Field(default_factory=list)
    # Why clarification_task was raised for a schedule: a conflict or a past time.
    schedule_reason: Literal["conflict", "past"] | None = None
    conversation_id: str | None = None


class AgentConversationMessageResponse(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


class AgentConversationResponse(BaseModel):
    conversation_id: str
    customer_id: int | None
    messages: list[AgentConversationMessageResponse]


class CustomerSummaryOutput(BaseModel):
    summary: str = Field(min_length=1)
    key_points: list[str] = Field(default_factory=list)
    customer_concerns: list[str] = Field(default_factory=list)
    recommended_next_action: str = Field(min_length=1)


class CustomerSummaryResponse(BaseModel):
    customer_id: int
    summary: str
    key_points: list[str]
    customer_concerns: list[str]
    open_followups: list[str]
    recommended_next_action: str
    generated_at: str


class MeetingBriefOutput(BaseModel):
    brief: str = Field(min_length=1)
    customer_overview: str = Field(min_length=1)
    current_requirement: str = Field(min_length=1)
    unresolved_issues: list[str] = Field(default_factory=list)
    recommended_talking_points: list[str] = Field(default_factory=list)
    recommended_next_action: str = Field(min_length=1)


class MeetingBriefResponse(BaseModel):
    meeting_id: int
    customer_id: int
    brief: str
    customer_overview: str
    current_requirement: str
    previous_discussions: list[str]
    unresolved_issues: list[str]
    recommended_talking_points: list[str]
    recommended_next_action: str
    generated_at: str
