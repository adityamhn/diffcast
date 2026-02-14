"""Feature demo video recording via browser-use."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run async coroutine from sync context (e.g. thread)."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def record_feature_demo_sync(
    website_url: str,
    feature_description: str,
    output_dir: str | Path,
    headless: bool = True,
) -> Path:
    """
    Record a feature demo video. Runs async browser-use in sync context.

    Args:
        website_url: URL of the app to demonstrate
        feature_description: Goal/description of the feature (from LLM)
        output_dir: Directory to save the video
        headless: Run browser headless (default True for servers)

    Returns:
        Path to the recorded .webm video file
    """
    return _run_async(
        _record_feature_demo_async(
            website_url=website_url,
            feature_description=feature_description,
            output_dir=Path(output_dir),
            headless=headless,
        )
    )


async def _record_feature_demo_async(
    website_url: str,
    feature_description: str,
    output_dir: Path,
    headless: bool,
) -> Path:
    from browser_use import Agent, Browser, ChatGoogle

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    record_dir = str(output_dir)

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is required for feature demo recording")

    llm = ChatGoogle(
        model="gemini-2.5-flash-lite",
        temperature=0.3,
        api_key=api_key,
    )

    browser = Browser(
        use_cloud=False,
        headless=headless,
        record_video_dir=record_dir,
        record_video_size={"width": 1280, "height": 720},
    )

    task = f"""
1. Navigate to {website_url}
2. Demonstrate this new feature: {feature_description}
3. Interact naturally with the new UI (e.g., type in search, use filters if present)
4. Keep the demonstration under 10 seconds
5. Focus only on the changed feature
"""

    agent = Agent(
        task=task,
        llm=llm,
        browser=browser,
        use_vision=True,
        max_actions_per_step=3,
    )

    try:
        await agent.run()
        video_path = _find_video_in_dir(output_dir)
        if not video_path:
            raise FileNotFoundError(
                f"No video file (.webm or .mp4) found in {output_dir} after recording"
            )
        logger.info("Feature demo recorded path=%s", video_path)
        return video_path
    finally:
        if hasattr(browser, "stop"):
            await browser.stop()


def _find_video_in_dir(directory: Path) -> Path | None:
    """Find the most recently modified video file (.webm or .mp4) in directory."""
    video_files = list(directory.glob("*.webm")) + list(directory.glob("*.mp4"))
    if not video_files:
        return None
    return max(video_files, key=lambda p: p.stat().st_mtime)
