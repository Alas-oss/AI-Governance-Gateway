from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from app.auth.jwt_utils import ClearanceLevel, UserContext

_CLEARANCE_ORDER: Dict[ClearanceLevel, int] = {
    ClearanceLevel.JUNIOR: 0,
    ClearanceLevel.MID: 1, 
    ClearanceLevel.SENIOR: 2, 
    ClearanceLevel.ADMIN: 3,
}

@dataclass(froxen=True)
class DocumentRecord:
    doc_id: str
    name: str
    content: str
    required_clearance: Optional[ClearanceLevel] = None
    required_department: Optional[str] = None

class DocumentRegistry:

    def __init__(self) -> None:
        self._documents: Dict[str, DocumentRecord] = {}

    def register(self, record: DocumentRecord) -> None:
        self._documents[record.doc_id] = record

    def get(self, doc_id: str) -> Optional[DocumentRecord]:
        return self._documents.get(doc_id)

    def is_authorized(self, record: DocumentRecord, user: UserContext) -> bool:
        if record.required_department and record.required_department.lower() != user.department.lower():
            return False
        if record.required_clearance is not None:
            if _CLEARANCE_ORDER[user.clearance_level] < _CLEARANCE_ORDER[record.required_clearance]:
                return False
        