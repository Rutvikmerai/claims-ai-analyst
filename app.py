"""
app.py

Streamlit front end for the natural-language claims query tool.

Flow:
  1. User types a plain-English question.
  2. Claude (via the Anthropic API) is given the DB schema and one tool,
     `run_sql_query`, which is actually served by our MCP server - not
     hardcoded here. Claude decides what SQL to write.
  3. The generated SQL is sent over MCP (stdio) to mcp_server.py, which
     executes it against SQLite and returns results.
  4. Claude turns the raw rows into a plain-English answer.
  5. Streamlit displays the answer, with the generated SQL available
     behind a "Show SQL" expander for transparency.

Run with:
    streamlit run app.py
"""

import asyncio
import os
import sys

import streamlit as st
from anthropic import Anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MODEL = "claude-sonnet-4-6"
SERVER_SCRIPT = os.path.join(os.path.dirname(__file__), "mcp_server.py")

st.set_page_config(page_title="Claims Data Assistant", page_icon="📊", layout="centered")


# ---------------------------------------------------------------------------
# MCP <-> Claude bridge
# ---------------------------------------------------------------------------
# Streamlit callbacks are synchronous, but the MCP client SDK is async.
# This helper spins up a fresh event loop, opens a connection to the MCP
# server as a subprocess, runs one full question/answer exchange with
# Claude, then tears the connection down. That per-question lifecycle
# keeps things simple and reliable for a demo; a production version would
# keep the session open across the app's lifetime instead.

async def ask_claims_assistant(question: str, client: Anthropic):
    server_params = StdioServerParameters(command=sys.executable, args=[SERVER_SCRIPT])

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Pull the live schema from the MCP Resource so Claude always
            # writes SQL against the real, current table structure.
            schema_resource = await session.read_resource("schema://claims_db")
            schema_text = schema_resource.contents[0].text

            # Translate the MCP tool definition into the format the
            # Claude API expects for tool use.
            mcp_tools = await session.list_tools()
            claude_tools = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.inputSchema,
                }
                for t in mcp_tools.tools
            ]

            system_prompt = f"""You are a healthcare claims data analyst assistant.
You have access to a SQLite database with this schema:

{schema_text}

When the user asks a business question, write a single read-only SELECT
query using the run_sql_query tool to answer it. Always use the tool -
never guess at numbers. After you get results back, explain the answer
in plain, non-technical business language. Mention specific numbers from
the results. Keep the final explanation to 3-4 sentences."""

            messages = [{"role": "user", "content": question}]
            generated_sql = None

            # Tool-use loop: Claude may call the tool, we execute it via
            # MCP, feed the result back, and repeat until Claude gives a
            # final text answer.
            for _ in range(4):  # hard cap so a misbehaving loop can't run forever
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=1024,
                    system=system_prompt,
                    tools=claude_tools,
                    messages=messages,
                )

                if response.stop_reason != "tool_use":
                    final_text = "".join(
                        block.text for block in response.content if block.type == "text"
                    )
                    return final_text, generated_sql

                messages.append({"role": "assistant", "content": response.content})

                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue

                    if block.name == "run_sql_query":
                        generated_sql = block.input.get("query")

                    result = await session.call_tool(block.name, block.input)
                    result_text = "".join(
                        c.text for c in result.content if hasattr(c, "text")
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_text,
                        }
                    )

                messages.append({"role": "user", "content": tool_results})

            return "I wasn't able to reach a final answer in time. Try rephrasing the question.", generated_sql


def run_query(question: str, api_key: str):
    client = Anthropic(api_key=api_key)
    return asyncio.run(ask_claims_assistant(question, client))


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

st.title("📊 Claims Data Assistant")
st.caption(
    "Ask questions about claims data in plain English. "
    "No SQL knowledge required - Claude writes and runs the query for you via MCP."
)

api_key = os.environ.get("ANTHROPIC_API_KEY", "")
if not api_key:
    api_key = st.text_input("Anthropic API key", type="password")

example_questions = [
    "Which claim types had the highest rejection rate?",
    "What is the average billed amount for inpatient claims?",
    "Which provider specialty has submitted the most claims?",
    "What are the top 3 rejection reasons this year?",
    "How much total revenue was paid out for pharmacy claims?",
]

# "Hard" questions - deliberately ambiguous, partially-answerable, or
# out-of-scope. Useful in interviews to show how the tool degrades
# gracefully instead of breaking when pushed past the happy path.
hard_questions = [
    "Which provider should we be worried about?",
    "What's driving the pharmacy rejections?",
    "Update claim 105's status to Paid.",
]

with st.expander("Try an example question"):
    for q in example_questions:
        if st.button(q, key=q):
            st.session_state["question"] = q

with st.expander("Try a harder / edge-case question"):
    st.caption(
        "These are deliberately ambiguous, partially answerable, or out of "
        "scope - useful for seeing how the assistant handles imperfect questions."
    )
    for q in hard_questions:
        if st.button(q, key=q):
            st.session_state["question"] = q

question = st.text_input(
    "Your question",
    value=st.session_state.get("question", ""),
    placeholder="e.g. Which claim types had the highest rejection rate last quarter?",
)

if st.button("Ask", type="primary", disabled=not api_key):
    if not question.strip():
        st.warning("Type a question first.")
    else:
        with st.spinner("Claude is writing and running the query..."):
            try:
                answer, sql = run_query(question, api_key)
            except Exception as e:
                st.error(f"Something went wrong: {e}")
            else:
                st.markdown("### Answer")
                st.write(answer)
                if sql:
                    with st.expander("Show generated SQL"):
                        st.code(sql, language="sql")

if not api_key:
    st.info("Enter your Anthropic API key above to start (or set ANTHROPIC_API_KEY as an environment variable).")
