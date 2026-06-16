"""Memory setup: short-term (checkpointer) + long-term (remember/recall tools).

Graceful fallback:
- AGENTBASE_MEMORY_ID set  → AgentBaseMemoryEvents (platform persistence) + remember/recall tools
- AGENTBASE_MEMORY_ID unset → MemorySaver (in-process, resets on restart)

Both modes maintain per-conversation history keyed by Teams conv_id (thread_id).
"""

import os
import logging
from typing import Any

logger = logging.getLogger(__name__)

MEMORY_ID: str = os.getenv("MEMORY_ID", "") or os.getenv("AGENTBASE_MEMORY_ID", "")
MEMORY_STRATEGY_ID: str = os.getenv("MEMORY_STRATEGY_ID", "default")


def get_checkpointer() -> Any:
    """Return checkpointer for conversation history.

    AgentBaseMemoryEvents with limit=2000 loads up to 2000 checkpoints per request,
    giving the LLM sufficient conversation history. Falls back to MemorySaver if not installed.
    """
    if MEMORY_ID:
        try:
            from greennode_agent_bridge import AgentBaseMemoryEvents
            # limit=50: load tối đa 50 checkpoint events (≈10 exchanges).
            # 2000 events cũ bị load hết → context overflow với file Excel lớn.
            checkpointer = AgentBaseMemoryEvents(memory_id=MEMORY_ID, limit=50)
            logger.info("Using AgentBaseMemoryEvents (memory_id=%s, limit=50)", MEMORY_ID)
            return checkpointer
        except ImportError:
            logger.warning("greennode-agent-bridge not installed; falling back to MemorySaver")
        except Exception as e:
            logger.warning("AgentBaseMemoryEvents init failed (%s); falling back to MemorySaver", e)
    from langgraph.checkpoint.memory import MemorySaver
    logger.info("Using MemorySaver (in-process, no persistence across restarts)")
    return MemorySaver()


def purge_bloated_sessions(threshold: int = 400) -> int:
    """Delete all events for sessions that exceed `threshold` events.

    Calls Memory Service API directly (list_actors → list_sessions → list_events →
    delete_event). Intended to run at startup to clear accumulated checkpoint events
    that AgentBaseMemoryEvents.put() adds on every invoke but RemoveMessage never
    removes from the backend.

    Returns the number of sessions purged.
    """
    if not MEMORY_ID:
        return 0
    try:
        from greennode_agentbase.memory import MemoryClient
        client = MemoryClient()
        purged = 0

        actor_page = 1
        while True:
            actors_resp = client.list_actors(id=MEMORY_ID, page=actor_page, size=100)
            actors = actors_resp.list_data or []
            if not actors:
                break

            for actor in actors:
                actor_id = actor.actor_id
                sess_page = 1
                while True:
                    sess_resp = client.list_sessions(
                        id=MEMORY_ID, actorId=actor_id, page=sess_page, size=100
                    )
                    sessions = sess_resp.list_data or []
                    if not sessions:
                        break

                    for session in sessions:
                        session_id = session.session_id
                        ev_resp = client.list_events(
                            id=MEMORY_ID, actorId=actor_id, sessionId=session_id,
                            page=1, size=1,
                        )
                        total_events = ev_resp.total_item or 0
                        if total_events > threshold:
                            logger.info(
                                "Purging session actor=%s session=%s total_events=%d",
                                actor_id, session_id, total_events,
                            )
                            deleted = _delete_session_events(client, actor_id, session_id)
                            logger.info("Purged %d events from session %s/%s", deleted, actor_id, session_id)
                            purged += 1

                    if sess_page >= (sess_resp.total_page or 1):
                        break
                    sess_page += 1

            if actor_page >= (actors_resp.total_page or 1):
                break
            actor_page += 1

        if purged:
            logger.info("Memory cleanup: purged %d bloated session(s)", purged)
        else:
            logger.info("Memory cleanup: no sessions exceeded threshold=%d", threshold)
        return purged

    except Exception as e:
        logger.warning("purge_bloated_sessions failed: %s", e)
        return 0


def _delete_session_events(client: Any, actor_id: str, session_id: str) -> int:
    """Delete all events for one session by repeatedly fetching page 1 until empty.

    Always refetches page 1 (instead of paginating forward) because deleted events
    shift the list — incrementing the page number would skip items.
    Guards against infinite loops: if a full batch yields zero successful deletions,
    stop to avoid spinning on persistently failing events.
    """
    deleted = 0
    while True:
        resp = client.list_events(
            id=MEMORY_ID, actorId=actor_id, sessionId=session_id, page=1, size=100
        )
        events = resp.list_data or []
        if not events:
            break
        batch_deleted = 0
        for event in events:
            if event.id:
                try:
                    client.delete_event(
                        id=MEMORY_ID, actorId=actor_id, sessionId=session_id,
                        eventId=event.id,
                    )
                    deleted += 1
                    batch_deleted += 1
                except Exception as e:
                    logger.warning("delete_event %s failed: %s", event.id, e)
        if batch_deleted == 0:
            # No progress made — all deletions failed; stop to avoid infinite loop
            logger.warning(
                "Stopping purge for session %s/%s after 0 successful deletions in batch",
                actor_id, session_id,
            )
            break
    return deleted


def get_memory_tools() -> list:
    """Return [remember, recall] tools when platform memory is configured, else []."""
    if not MEMORY_ID:
        logger.info("MEMORY_ID not set — long-term memory tools disabled")
        return []

    try:
        from langchain_core.tools import tool
        from langgraph.config import get_config
        from greennode_agentbase.memory import MemoryClient
        from greennode_agentbase.memory.models import MemoryRecordSearchRequest

        _client = MemoryClient()

        def _actor_id() -> str:
            """Get actor_id from LangGraph configurable (injected at invoke time)."""
            try:
                cfg = get_config()
                return cfg["configurable"].get("actor_id", "default")
            except Exception:
                return "default"

        def _ns(actor_id: str) -> str:
            return f"/strategies/{MEMORY_STRATEGY_ID}/actors/{actor_id}"

        @tool
        def remember(fact: str) -> str:
            """Lưu một thông tin quan trọng về user vào bộ nhớ dài hạn để nhớ sau này.

            Args:
                fact: Thông tin cần ghi nhớ (sở thích, thói quen, yêu cầu thường xuyên...).
            """
            ns = _ns(_actor_id())
            _client.insert_memory_records_directly(
                id=MEMORY_ID, namespace=ns, request={"memories": [fact]}
            )
            return f"✅ Đã ghi nhớ: {fact}"

        @tool
        def recall(query: str) -> str:
            """Tìm kiếm thông tin đã lưu về user liên quan đến một truy vấn.

            Args:
                query: Câu truy vấn ngôn ngữ tự nhiên để tìm thông tin liên quan.
            """
            ns = _ns(_actor_id())
            results = _client.search_memory_records(
                id=MEMORY_ID,
                namespace=ns,
                request=MemoryRecordSearchRequest(query=query, limit=10),
            )
            if not results:
                return "Không tìm thấy thông tin liên quan trong bộ nhớ."
            return "\n".join(f"- {r.memory} (score: {r.score:.2f})" for r in results)

        logger.info("Long-term memory tools enabled (strategy=%s)", MEMORY_STRATEGY_ID)
        return [remember, recall]

    except ImportError as e:
        logger.warning("Memory tools unavailable (import error: %s)", e)
        return []
    except Exception as e:
        logger.warning("Memory tools init failed: %s", e)
        return []
