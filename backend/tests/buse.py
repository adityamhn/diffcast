from browser_use import Agent, Browser, ChatBrowserUse
import asyncio

from browser_use.llm.google.chat import ChatGoogle


async def example():
    browser = Browser(
        use_cloud=False,  # Uncomment to use a stealth browser on Browser Use Cloud
    )

    llm = ChatGoogle(model="gemini-2.0-flash-exp", temperature=0.3)

    agent = Agent(
        task="Find the number of stars of the browser-use repo",
        llm=llm,
        browser=browser,
    )

    history = await agent.run()
    return history


if __name__ == "__main__":
    history = asyncio.run(example())
    print(history)