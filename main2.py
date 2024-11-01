from langchain_core.messages.base import BaseMessage
from langgraph_sdk import get_client
from data_fetchers import fetch_active_markets
from langgraph_sdk.schema import Thread
import asyncio
from langchain_community.document_loaders import WikipediaLoader
from langchain_community.tools.tavily_search import TavilySearchResults
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    get_buffer_string,
)
from langchain_openai import ChatOpenAI

from langgraph.constants import Send
from langgraph.graph import END, START, StateGraph

from models import (
    Market,
    GenerateAnalystsState,
    InterviewState,
    Perspectives,
    Recommendation,
    ResearchGraphState,
    SearchQuery,
    TraderState,
)
from trade_tools import get_balances, trade_execution

### LLM

llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)


### Nodes and edges


def format_market_odds(market: Market) -> str:
    """Format market odds into a readable string"""
    return {
        outcome: price for outcome, price in zip(market.outcomes, market.outcome_prices)
    }


analyst_instructions = """You are tasked with creating a set of AI analyst personas. Follow these instructions carefully:

1. First, review the prediction market details:

Market Question: {market.question}
Description: {market.description}
Current Odds: {market_odds}
End Date: {market.end_date}
Volume: {market.volume}
    
2. Determine the most interesting themes based on the market details and feedback above.
                    
3. Pick the top {max_analysts} themes.

4. Assign one analyst to each theme. Each analyst should focus on a different aspect that could affect the market outcome."""


def create_analysts(state: GenerateAnalystsState):
    """Create analysts"""

    market = state.market
    max_analysts = state.max_analysts

    # Format market odds
    market_odds = format_market_odds(market)

    # Enforce structured output
    structured_llm = llm.with_structured_output(Perspectives)

    # System message
    system_message = analyst_instructions.format(
        market=market,
        market_odds=market_odds,
        max_analysts=max_analysts,
    )

    # Generate question
    analysts = structured_llm.invoke(
        [SystemMessage(content=system_message)]
        + [HumanMessage(content="Generate the set of analysts.")]
    )

    # Write the list of analysis to state
    return {"analysts": analysts.analysts}


# Generate analyst question
question_instructions = """You are an analyst tasked with interviewing an expert to learn about a specific topic. 

Your goal is boil down to interesting and specific insights related to your topic.

1. Interesting: Insights that people will find surprising or non-obvious.
        
2. Specific: Insights that avoid generalities and include specific examples from the expert.

Here is your topic of focus and set of goals: {goals}
        
Begin by introducing yourself using a name that fits your persona, and then ask your question.

Continue to ask questions to drill down and refine your understanding of the topic.
        
When you are satisfied with your understanding, complete the interview with: "Thank you so much for your help!"

Remember to stay in character throughout your response, reflecting the persona and goals provided to you."""


def generate_question(state: InterviewState):
    """Node to generate a question"""

    # Get state
    analyst = state["analyst"]
    messages = state["messages"]

    # Generate question
    system_message = question_instructions.format(goals=analyst.persona)
    question = llm.invoke([SystemMessage(content=system_message)] + messages)

    # Write messages to state
    return {"messages": [question]}


# Search query writing
search_instructions = SystemMessage(
    content="""You will be given a conversation between an analyst and an expert. 

Your goal is to generate a well-structured query for use in retrieval and / or web-search related to the conversation.
        
First, analyze the full conversation.

Pay particular attention to the final question posed by the analyst.

Convert this final question into a well-structured web search query"""
)


def search_web(state: InterviewState):
    """Retrieve docs from web search"""

    # Search
    tavily_search = TavilySearchResults(max_results=3)

    # Search query
    structured_llm = llm.with_structured_output(SearchQuery)
    search_query = structured_llm.invoke([search_instructions] + state["messages"])

    # Search
    search_docs = tavily_search.invoke(search_query.search_query)

    # Format
    formatted_search_docs = "\n\n---\n\n".join(
        [
            f'<Document href="{doc["url"]}"/>\n{doc["content"]}\n</Document>'
            for doc in search_docs
        ]
    )

    return {"context": [formatted_search_docs]}


def search_wikipedia(state: InterviewState):
    """Retrieve docs from wikipedia"""

    # Search query
    structured_llm = llm.with_structured_output(SearchQuery)
    search_query = structured_llm.invoke([search_instructions] + state["messages"])

    # Search
    search_docs = WikipediaLoader(
        query=search_query.search_query, load_max_docs=2
    ).load()

    # Format
    formatted_search_docs = "\n\n---\n\n".join(
        [
            f'<Document source="{doc.metadata["source"]}" page="{doc.metadata.get("page", "")}"/>\n{doc.page_content}\n</Document>'
            for doc in search_docs
        ]
    )

    return {"context": [formatted_search_docs]}


# Generate expert answer
answer_instructions = """You are an expert being interviewed by an analyst.

Here is analyst area of focus: {goals}. 
        
You goal is to answer a question posed by the interviewer.

To answer question, use this context:
        
{context}

When answering questions, follow these guidelines:
        
1. Use only the information provided in the context. 
        
2. Do not introduce external information or make assumptions beyond what is explicitly stated in the context.

3. The context contain sources at the topic of each individual document.

4. Include these sources your answer next to any relevant statements. For example, for source # 1 use [1]. 

5. List your sources in order at the bottom of your answer. [1] Source 1, [2] Source 2, etc
        
6. If the source is: <Document source="assistant/docs/llama3_1.pdf" page="7"/>' then just list: 
        
[1] assistant/docs/llama3_1.pdf, page 7 
        
And skip the addition of the brackets as well as the Document source preamble in your citation."""


def generate_answer(state: InterviewState):
    """Node to answer a question"""

    # Get state
    analyst = state["analyst"]
    messages = state["messages"]
    context = state["context"]

    # Answer question
    system_message = answer_instructions.format(goals=analyst.persona, context=context)
    answer = llm.invoke([SystemMessage(content=system_message)] + messages)

    # Name the message as coming from the expert
    answer.name = "expert"

    # Append it to state
    return {"messages": [answer]}


def save_interview(state: InterviewState):
    """Save interviews"""

    # Get messages
    messages = state["messages"]

    # Convert interview to a string
    interview = get_buffer_string(messages)

    # Save to interviews key
    return {"interview": interview}


def route_messages(state: InterviewState, name: str = "expert"):
    """Route between question and answer"""

    # Get messages
    messages = state["messages"]
    max_num_turns = state.get("max_num_turns", 2)

    # Check the number of expert answers
    num_responses = len(
        [m for m in messages if isinstance(m, AIMessage) and m.name == name]
    )

    # End if expert has answered more than the max turns
    if num_responses >= max_num_turns:
        return "save_interview"

    # This router is run after each question - answer pair
    # Get the last question asked to check if it signals the end of discussion
    last_question = messages[-2]

    if "Thank you so much for your help" in last_question.content:
        return "save_interview"
    return "ask_question"


# Write a summary (section of the final report) of the interview
section_writer_instructions = """You are an expert technical writer. 
            
Your task is to create a short, easily digestible section of a report based on a set of source documents.

1. Analyze the content of the source documents: 
- The name of each source document is at the start of the document, with the <Document tag.

2. Create a report structure using markdown formatting:
- Use ## for the section title
- Use ### for sub-section headers
        
3. Write the report following this structure:
a. Title (## header)
b. Summary (### header)
c. Sources (### header)

4. Make your title engaging based upon the focus area of the analyst: 
{focus}

5. For the summary section:
- Set up summary with general background / context related to the focus area of the analyst
- Emphasize what is novel, interesting, or surprising about insights gathered from the interview
- Create a numbered list of source documents, as you use them
- Do not mention the names of interviewers or experts
- Aim for approximately 400 words maximum
- Use numbered sources in your report (e.g., [1], [2]) based on information from source documents
        
6. In the Sources section:
- Include all sources used in your report
- Provide full links to relevant websites or specific document paths
- Separate each source by a newline. Use two spaces at the end of each line to create a newline in Markdown.
- It will look like:

### Sources
[1] Link or Document name
[2] Link or Document name

7. Be sure to combine sources. For example this is not correct:

[3] https://ai.meta.com/blog/meta-llama-3-1/
[4] https://ai.meta.com/blog/meta-llama-3-1/

There should be no redundant sources. It should simply be:

[3] https://ai.meta.com/blog/meta-llama-3-1/
        
8. Final review:
- Ensure the report follows the required structure
- Include no preamble before the title of the report
- Check that all guidelines have been followed"""


def write_section(state: InterviewState):
    """Node to write a section"""

    # Get state
    interview = state["interview"]
    context = state["context"]
    analyst = state["analyst"]

    # Write section using either the gathered source docs from interview (context) or the interview itself (interview)
    system_message = section_writer_instructions.format(focus=analyst.description)
    section = llm.invoke(
        [SystemMessage(content=system_message)]
        + [HumanMessage(content=f"Use this source to write your section: {context}")]
    )

    # Append it to state
    return {"sections": [section.content]}


# Add nodes and edges
interview_builder = StateGraph(InterviewState)
interview_builder.add_node("ask_question", generate_question)
interview_builder.add_node("search_web", search_web)
interview_builder.add_node("search_wikipedia", search_wikipedia)
interview_builder.add_node("answer_question", generate_answer)
interview_builder.add_node("save_interview", save_interview)
interview_builder.add_node("write_section", write_section)

# Flow
interview_builder.add_edge(START, "ask_question")
interview_builder.add_edge("ask_question", "search_web")
interview_builder.add_edge("ask_question", "search_wikipedia")
interview_builder.add_edge("search_web", "answer_question")
interview_builder.add_edge("search_wikipedia", "answer_question")
interview_builder.add_conditional_edges(
    "answer_question", route_messages, ["ask_question", "save_interview"]
)
interview_builder.add_edge("save_interview", "write_section")
interview_builder.add_edge("write_section", END)


def initiate_all_interviews(state: ResearchGraphState):
    """Conditional edge to initiate all interviews via Send() API or return to create_analysts"""

    # Check if human feedback
    # Otherwise kick off interviews in parallel via Send() API
    market = state.market
    return [
        Send(
            "conduct_interview",
            {
                "analyst": analyst,
                "messages": [
                    HumanMessage(
                        content=f"So you said you were analyzing the market question: {market.question}?"
                    )
                ],
            },
        )
        for analyst in state.analysts
    ]


recommendation_instructions = """You are an expert market analyst creating a comprehensive report on this prediction market:

Market Question: {market.question}
Description: {market.description}
Current Odds: {market_odds}
End Date: {market.end_date}
Volume: {market.volume}
    
You have a team of analysts. Each analyst has done two things: 

1. They conducted an interview with an expert on a specific prediction market.
2. They write up their findings into a memo.

Your task: 

1. You will be given a collection of memos from your analysts, along with the current odds for the prediction market.
2. Think carefully about the insights from each memo.
3. Based on the provided odds and your analysis of the memos, make a recommendation on whether to buy one of the outcomes, or do nothing. 
Also provide your conviction score for the recommendation, where the conviction score is defined as how confident you are that buying this outcome has relative edge over the odds.

Here are the memos from your analysts to build your report from:
{context}
"""


def write_recommendation(state: ResearchGraphState):
    """Node to write the recommendation"""

    # Full set of sections
    sections = state.sections
    market = state.market
    market_odds = format_market_odds(market)

    # Concat all sections together
    formatted_str_sections = "\n\n".join([f"{section}" for section in sections])

    # Get recommendation using structured output
    system_message = recommendation_instructions.format(
        market=market, market_odds=market_odds, context=formatted_str_sections
    )
    recommendation = llm.with_structured_output(Recommendation).invoke(
        [SystemMessage(content=system_message)]
        + [HumanMessage(content="Create a recommendation based upon these memos.")]
    )

    # Return as a dict for state update
    return {"recommendation": recommendation}


trader_instructions = """You are an expert trader tasked with executing a trade based on a recommendation.
Here is the market data: {market}
Here is a recommendation: {recommendation}

Your task is to execute the trade based on the recommendation and the current balances provided.
Use the trade_execution tool to execute the trade, you'll need to format the trade details as an OrderDetails object.
For the token_id, use the clob_token_id from the market data. They correspond to the outcomes."""


def trader_execution(state: TraderState):
    """Node to execute the trader's recommendation"""

    market = state.market
    recommendation = state.recommendation
    llm_trader = llm.bind_tools([trade_execution])

    trade: BaseMessage = llm_trader.invoke(
        [
            SystemMessage(
                content=trader_instructions.format(
                    market=market, recommendation=recommendation
                )
            )
        ]
        + [
            HumanMessage(
                content="Execute the trade, and then output the order response as an OrderResponse object."
            )
        ],
    )
    return {"order_response": trade.content}


def performance_review(state: ResearchGraphState):
    """Node to review the performance of the traders"""

    return {"performance": "good"}


# Add nodes and edges
builder = StateGraph(ResearchGraphState)
builder.add_node("create_analysts", create_analysts)
builder.add_node("conduct_interview", interview_builder.compile())
builder.add_node("write_recommendation", write_recommendation)
builder.add_node("check_balances", get_balances)
builder.add_node("trader_execution", trader_execution)
builder.add_node("performance_review", performance_review)
# Logic
builder.add_edge(START, "create_analysts")
builder.add_conditional_edges(
    "create_analysts", initiate_all_interviews, ["conduct_interview"]
)
builder.add_edge("conduct_interview", "write_recommendation")
builder.add_edge("write_recommendation", "check_balances")
builder.add_edge("check_balances", "trader_execution")
builder.add_edge("trader_execution", "performance_review")
builder.add_edge("performance_review", END)


# Compile
graph = builder.compile()


async def main():
    URL = "http://localhost:62630"
    client = get_client(url=URL)

    thread: Thread = await client.threads.create()

    market = fetch_active_markets()[0]  # Get first active market
    initial_state = GenerateAnalystsState(market=market, max_analysts=1, analysts=[])

    run = await client.runs.create(
        thread_id=thread["thread_id"],
        assistant_id="research_agent2",
        input=initial_state.model_dump(),  # Use model_dump() for serialization
    )
    print(run)


if __name__ == "__main__":
    asyncio.run(main())
