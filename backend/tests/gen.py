from browser_use import Agent, Browser, ChatGoogle
from dotenv import load_dotenv
import asyncio

load_dotenv()


class FeatureVideoRecorder:
    def __init__(self):
        self.llm = ChatGoogle(model="gemini-2.0-flash-exp", temperature=0.3)

    async def record_feature_demo(
        self,
        website_url: str,
        feature_description: str,
        output_path: str = "feature_demo.webm",
    ) -> str:
        """
        Records a video of the new feature based on description

        Args:
            website_url: URL of your testing environment
            feature_description: What changed (from git diff analysis)
            output_path: Where to save the video

        Returns:
            Path to the recorded video
        """

        # Create browser with video recording enabled
        browser = Browser(
            use_cloud=False,
            headless=False,  # Set to True for headless environments
            record_video_dir="./videos",
            record_video_size={"width": 1280, "height": 720},
        )

        # Create a specific task based on the feature
        task = f"""
        1. Navigate to {website_url}
        2. Demonstrate this new feature: {feature_description}
        3. Interact with the new UI elements naturally (click buttons, use dropdowns, etc.)
        4. Keep the demonstration under 10 seconds
        5. Focus only on the changed feature
        """

        agent = Agent(
            task=task,
            llm=self.llm,
            browser=browser,
            use_vision=True,  # Helps agent see UI visually
            max_actions_per_step=3,
        )

        try:
            # Run the agent
            await agent.run()

            # Get video path (Playwright saves it automatically)
            video_path = await self._get_video_path(browser)

            return video_path

        finally:
            # BrowserSession uses stop()/kill(), not close()
            if hasattr(browser, "stop"):
                await browser.stop()

    async def _get_video_path(self, browser):
        # Playwright saves videos on context close
        # Path will be in the record_video_dir
        return "./videos/feature_demo.webm"


if __name__ == "__main__":
    asyncio.run(
        FeatureVideoRecorder().record_feature_demo(
            "https://bugbase.ai", "Add a new feature which includes new filters, showcase the new filters"
        )
    )
