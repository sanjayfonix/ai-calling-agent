"""
Dynamic collection flow service - fetches question configuration from backend API
"""

import httpx
import structlog
from typing import Dict, List, Any
from datetime import datetime, timedelta

logger = structlog.get_logger(__name__)

# Cache for dynamic flows to avoid hitting API every call
_flow_cache: Dict[str, Any] = {}
_cache_timestamp: datetime | None = None
_cache_ttl_seconds = 300  # 5 minutes

DYNAMIC_FLOW_API_URL = "https://xd363v4j-5000.inc1.devtunnels.ms/api/v1/admin/ai-collection-flows/getActiveAiCollectionFlows"


async def fetch_dynamic_collection_flow() -> Dict[str, Any] | None:
    """
    Fetch the active AI collection flow configuration from backend API.
    Returns the flow configuration or None if fetch fails.
    Uses caching to reduce API calls.
    """
    global _flow_cache, _cache_timestamp
    
    # Check cache
    if _flow_cache and _cache_timestamp:
        age = (datetime.now() - _cache_timestamp).total_seconds()
        if age < _cache_ttl_seconds:
            logger.info("dynamic_flow_cache_hit", age_seconds=age)
            return _flow_cache
    
    # Fetch from API
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(DYNAMIC_FLOW_API_URL)
            
            if response.status_code == 200:
                data = response.json()
                _flow_cache = data
                _cache_timestamp = datetime.now()
                logger.info(
                    "dynamic_flow_fetched",
                    status="success",
                    flows_count=len(data.get("data", [])) if isinstance(data.get("data"), list) else 0
                )
                return data
            else:
                logger.error(
                    "dynamic_flow_fetch_failed",
                    status_code=response.status_code,
                    response=response.text[:200]
                )
                return None
    except Exception as e:
        logger.error("dynamic_flow_fetch_error", error=str(e))
        return None


def build_dynamic_prompt(flow_data: Dict[str, Any] | None, base_prompt: str) -> str:
    """
    Generate a dynamic system prompt based on the flow configuration.
    If no flow data available, returns the base prompt.
    
    Args:
        flow_data: The flow configuration from the API
        base_prompt: The default/base prompt to use as fallback
        
    Returns:
        The customized prompt including dynamic questions
    """
    if not flow_data or "data" not in flow_data:
        logger.info("no_dynamic_flow_using_base_prompt")
        return base_prompt
    
    flows = flow_data.get("data", [])
    if not flows or not isinstance(flows, list):
        return base_prompt
    
    # Get the first active flow (assuming single active flow)
    active_flow = flows[0] if flows else None
    if not active_flow:
        return base_prompt
    
    questions = active_flow.get("questions", [])
    if not questions:
        logger.info("no_questions_in_flow_using_base")
        return base_prompt
    
    # Build the dynamic questions section
    questions_text = []
    for idx, q in enumerate(questions, 1):
        question_text = q.get("question", "")
        question_type = q.get("type", "text")
        required = q.get("required", False)
        
        if question_text:
            questions_text.append(f"{idx}) {question_text}")
            
            # Add validation hints based on type
            if question_type == "email":
                questions_text.append("- Must contain @ and a domain.")
            elif question_type == "phone":
                questions_text.append("- Must be a valid 10-digit US phone number.")
            elif question_type == "zipcode":
                questions_text.append("- Must be exactly 5 digits.")
            elif question_type == "number":
                questions_text.append(f"- Must be a valid number.")
            
            questions_text.append("")  # Empty line between questions
    
    # Replace the STRUCTURED DATA COLLECTION FLOW section in base prompt
    dynamic_questions_block = "\n".join(questions_text)
    
    # Find and replace the questions section
    start_marker = "========================\nSTRUCTURED DATA COLLECTION FLOW\n========================"
    end_marker = "========================\nACA EXPLANATION"
    
    if start_marker in base_prompt and end_marker in base_prompt:
        start_idx = base_prompt.find(start_marker)
        end_idx = base_prompt.find(end_marker)
        
        new_prompt = (
            base_prompt[:start_idx + len(start_marker)] + 
            "\nAsk ONE question at a time.\nWait for the answer before continuing.\nAcknowledge briefly before moving to the next question.\nDo not re-ask completed fields unless correction is required.\n\n" + 
            dynamic_questions_block + 
            "\n" + base_prompt[end_idx:]
        )
        
        logger.info("dynamic_prompt_generated", questions_count=len(questions))
        return new_prompt
    
    logger.warning("could_not_inject_dynamic_questions_using_base")
    return base_prompt


def extract_question_fields(flow_data: Dict[str, Any] | None) -> List[str]:
    """
    Extract the list of field names from the flow configuration.
    Used to know which fields to collect and send back.
    """
    if not flow_data or "data" not in flow_data:
        return []
    
    flows = flow_data.get("data", [])
    if not flows or not isinstance(flows, list):
        return []
    
    active_flow = flows[0] if flows else None
    if not active_flow:
        return []
    
    questions = active_flow.get("questions", [])
    field_names = []
    
    for q in questions:
        field_name = q.get("fieldName", "")
        if field_name:
            field_names.append(field_name)
    
    return field_names
