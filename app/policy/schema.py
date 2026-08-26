from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from app.auth.jwt_utils import ClearanceLevel

class ClearancePolicy(BaseModel):
    allowed_tools: List[str] = Field(
        default_factory=list, description="Exact tool/function names permitted for this clearance level."
    )
    allowed_tool_prefixes: List[str] = Field(
        default_factory=list, 
        description="Tool/function names are also allowed if they start with any of these prefixes -- "
        "useful for namespaced MCP tools like 'db.readonly.*'.",
    )
    restricted_doc_tags: List[str] = Field(
        default_factory=list,
        description="Sytem-prompt fragments wrapped in these markers, e.g. "
        "[INTERNAL-SENIOR-ONLY]...[/INTERNAL-SENIOR-ONLY], are entirely (reserved for 'admin')."
    )
    masking_exempt_entities: List[str] = Field(
        default_factory=list,
        description="Presidio entity types (e.g. 'BANK_CARD_NUMBER') that this clearance level is "
        "exempt from having masked -- for roles that have a legitimate, authorized need to see "
        "that category of data as part of their job (e.g. HR viewing an employee's card on file).",
    )

class DepartmentOverride(BaseModel):
    additional_restricted_doc_tags: List[str] = Field(default_factory=list)
    additional_masking_exempt_entities: List[str] = Field(
        default_factory=list, 
        description="Entity types exempts from masking specifically for this department+clearance "
        "combination -- e.g. HR staff viewing payment-method data as part of their actual job.",
    )

class PermissionMatrix(BaseModel):
    policies: Dict[ClearanceLevel, ClearancePolicy]
    default_policy: ClearancePolicy = Field(
        default_factory=ClearancePolicy,
        description="Fallback policy applied if a clearance level has no explicit entry.",
    )
    department_policies: Dict[str, Dict[ClearanceLevel, DepartmentOverride]] = Field(
        default_factory=dict,
        description="Per-department overlays, keyed by lowercase department name them clearance " \
        "level. Only present where a department needs restrictions beyond the base clearance policy.",
    )

    def policy_for(self, clearance_level: ClearanceLevel, department: Optional[str] = None) -> ClearancePolicy:
        base = self.policies.get(clearance_level, self.default_policy)

        if not department:
            return base

        override = self.department_policies.get(department.lower(), {}).get(clearance_level)
        if override is None:
            return base

        merged_tags = list(dict.fromkeys(base.restricted_doc_tags + override.additional_restricted_doc_tags))
        merged_exempt = list(
            dict.fromkeys(base.masking_exempt_entities + override.additional_masking_exempt_entities)
        )
        return base.model_copy(update={"restricted_doc_tags": merged_tags, "masking_exempt_entities": merged_exempt})