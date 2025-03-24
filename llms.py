### LLMs
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic

geminiflash = ChatGoogleGenerativeAI(
    model="gemini-2.0-flash",
    temperature=0,
)
gpt4o = ChatOpenAI(model="gpt-4o", temperature=0)
claude37 = ChatAnthropic(model="claude-3-7-sonnet-latest", temperature=0)
