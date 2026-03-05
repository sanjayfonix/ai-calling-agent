"""
Call Context - stores dynamic data for each call session.
This data comes from the Express backend and personalizes the AI agent.
"""

from __future__ import annotations

from datetime import datetime
from typing import List
from pydantic import BaseModel


class CallContext(BaseModel):
    """Dynamic context data for a single call."""
    
    agent_id: int
    agent_name: str
    agent_email: str
    agent_phone: str  
    agent_npn: str
    agent_role: str
    plan_name: str
    slots: List[str]  # Available slots in format: "2026-03-05|09:00"
    slots_count: int
    booked_slots: List[str] = []  # Slots already booked
    callback_url: str = ""  # URL to POST call results when call completes
    to_number: str = ""  # Phone number being called
    
    @property
    def available_slots(self) -> List[str]:
        """Get slots that are not booked."""
        return [slot for slot in self.slots if slot not in self.booked_slots]
    
    def is_slot_available(self, requested_slot: str) -> bool:
        """Check if a specific slot is available."""
        # Normalize the requested slot format
        # Handle various formats: "9 AM tomorrow", "9:00 AM", "09:00", etc.
        return requested_slot in self.available_slots
    
    def parse_slot_datetime(self, slot: str) -> tuple[str, str]:
        """Parse slot string into date and time."""
        # Format: "2026-03-05|09:00"
        if "|" in slot:
            date_str, time_str = slot.split("|")
            return date_str, time_str
        return "", ""
    
    def format_slots_for_prompt(self) -> str:
        """Format available slots in a readable way for the AI prompt."""
        if not self.available_slots:
            return "No slots currently available."
        
        slots_by_date = {}
        for slot in self.available_slots:
            date_str, time_str = self.parse_slot_datetime(slot)
            if date_str not in slots_by_date:
                slots_by_date[date_str] = []
            slots_by_date[date_str].append(time_str)
        
        formatted = []
        for date, times in sorted(slots_by_date.items()):
            formatted.append(f"{date}: {', '.join(times)}")
        
        return "\n".join(formatted)


# Global store for call contexts (keyed by call_sid)
_call_contexts: dict[str, CallContext] = {}


def store_call_context(call_sid: str, context: CallContext) -> None:
    """Store context for a call."""
    _call_contexts[call_sid] = context


def get_call_context(call_sid: str) -> CallContext | None:
    """Retrieve context for a call."""
    return _call_contexts.get(call_sid)


def remove_call_context(call_sid: str) -> None:
    """Clean up context after call ends."""
    _call_contexts.pop(call_sid, None)
