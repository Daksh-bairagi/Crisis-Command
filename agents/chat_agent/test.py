import asyncio
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part

from agents.chat_agent import create_chat_agent


def _extract_text(event) -> str:
    if not getattr(event, "content", None) or not event.content.parts:
        return ""

    texts = [part.text for part in event.content.parts if getattr(part, "text", None)]
    return "\n".join(texts)


async def test():
    agent = create_chat_agent()
    session_service = InMemorySessionService()
    runner = Runner(agent=agent, app_name='test', session_service=session_service)

    session = await session_service.create_session(app_name='test', user_id='test_user')

    result = runner.run_async(
        user_id='test_user',
        session_id=session.id,
        new_message=Content(
            parts=[
                Part(
                     text='Post an incident alert with these details: incident_id=INC-TEST01, severity=P0, service=payments-service, description=500 errors on checkout, likely_cause=Bad deployment deploy-447, suggested_action=Rollback deploy-447, affected_users=12000'
                )
            ]
        )
    )

    try:
        async for event in result:
            if event.is_final_response():
                print('Agent response:', _extract_text(event))
    except Exception as exc:
        print(f"Test failed: {type(exc).__name__}: {exc}")
        raise

if __name__ == "__main__":
    asyncio.run(test())
