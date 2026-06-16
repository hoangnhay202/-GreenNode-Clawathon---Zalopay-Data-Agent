import os
from typing import Optional

from langchain_openai import ChatOpenAI


def get_llm_model() -> ChatOpenAI:
    """
    Build a LangChain ChatOpenAI model with environment configuration.
    
    Environment variables (priority order):
    - LLM_API_KEY or OPENAI_API_KEY: API key for LLM provider
    - LLM_BASE_URL or OPENAI_API_BASE: Base URL (default: https://api.openai.com/v1)
    - LLM_MODEL: Model name (default: gpt-3.5-turbo)
    
    GreenNode AI Platform setup (recommended):
      - LLM_API_KEY: from /agentbase-llm or /agentbase-identity
      - LLM_BASE_URL: https://maas-llm-aiplatform-hcm.api.vngcloud.vn/v1
      - LLM_MODEL: model path from /agentbase-llm models list
    """
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_API_BASE") or "https://api.openai.com/v1"
    model = os.getenv("LLM_MODEL", "gpt-3.5-turbo")

    if not api_key:
        raise ValueError(
            "LLM_API_KEY (or OPENAI_API_KEY) not set. "
            "Please configure via /agentbase-llm or set in .env"
        )

    # Don't set max_tokens — let the provider enforce its own limit.
    # Hardcoding max_tokens=4096 causes the server to return -N when the prompt
    # already consumes most of the context window (max_tokens = limit - prompt - 4096 < 0).
    return ChatOpenAI(
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=0.7,
    )
