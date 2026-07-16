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
from datetime import date
from pathlib import Path

import streamlit as st
from anthropic import Anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MODEL = "claude-sonnet-4-6"
SERVER_SCRIPT = os.path.join(os.path.dirname(__file__), "mcp_server.py")
DEFAULT_DB_PATH = Path(__file__).parent / "db" / "claims.db"
MAX_QUESTIONS_PER_SESSION = 3       # per-visitor cap, resets if they reload the page
MAX_QUESTIONS_PER_DAY_GLOBAL = 50  # shared cap across ALL visitors, resets at midnight UTC
USAGE_FILE = Path(__file__).parent / ".daily_usage.txt"

st.set_page_config(page_title="Claims Data Assistant", page_icon="📊", layout="centered")


def get_api_key() -> str:
    """
    Looks for the API key in this order:
      1. Streamlit secrets (st.secrets) - used when deployed on Streamlit
         Community Cloud, where you set ANTHROPIC_API_KEY under app settings
         -> Secrets. Visitors never see or need this - this is what makes
         the app usable by strangers on LinkedIn without them needing a key.
      2. Environment variable - used for local `streamlit run` testing.
      3. Manual text input - last-resort fallback so the app still works
         if neither of the above is set (e.g. someone clones the repo and
         wants to use their own key instead of yours).
    """
    try:
        if "ANTHROPIC_API_KEY" in st.secrets:
            return st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        pass  # st.secrets raises if no secrets.toml exists at all - that's fine

    env_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if env_key:
        return env_key

    return st.text_input("Anthropic API key", type="password")


def check_global_daily_limit() -> tuple[bool, int]:
    """
    Tracks total questions asked across ALL visitors today, in a plain text
    file next to the app. This is the safety net for a publicly shared demo
    running on YOUR API key - without it, one person (or a bot) could send
    thousands of requests and run up your bill overnight.

    Returns (allowed, count_so_far). Resets automatically when the date changes.
    Good enough for a portfolio demo; a production app would use a real
    database or Redis counter instead of a text file.
    """
    today = date.today().isoformat()
    count = 0
    if USAGE_FILE.exists():
        stored_date, _, stored_count = USAGE_FILE.read_text().partition(",")
        if stored_date == today:
            count = int(stored_count or 0)

    if count >= MAX_QUESTIONS_PER_DAY_GLOBAL:
        return False, count

    USAGE_FILE.write_text(f"{today},{count + 1}")
    return True, count + 1


# ---------------------------------------------------------------------------
# MCP <-> Claude bridge
# ---------------------------------------------------------------------------
# Streamlit callbacks are synchronous, but the MCP client SDK is async.
# This helper spins up a fresh event loop, opens a connection to the MCP
# server as a subprocess, runs one full question/answer exchange with
# Claude, then tears the connection down. That per-question lifecycle
# keeps things simple and reliable for a demo; a production version would
# keep the session open across the app's lifetime instead.

async def ask_claims_assistant(question: str, client: Anthropic, db_path: str | None = None):
    args = [SERVER_SCRIPT] if db_path is None else [SERVER_SCRIPT, db_path]
    server_params = StdioServerParameters(command=sys.executable, args=args)

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Pull the live schema from the MCP Resource so Claude always
            # writes SQL against the real, current table structure - this
            # works identically whether we're serving the sample claims DB
            # or a database built from a user's uploaded CSV.
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

            domain_hint = (
                "a healthcare claims database"
                if db_path is None
                else "a database built from a user-uploaded CSV file - infer the "
                "business meaning of the columns from their names"
            )

            system_prompt = f"""You are a data analyst assistant. You have access to
{domain_hint} with this schema:

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


def run_query(question: str, api_key: str, db_path: str | None = None):
    client = Anthropic(api_key=api_key)
    return asyncio.run(ask_claims_assistant(question, client, db_path))


MAX_CSV_ROWS = 20_000  # keep uploaded data small enough to stay fast and cheap to query


def build_db_from_csv(uploaded_file) -> tuple[str | None, str | None]:
    """
    Converts an uploaded CSV into a temporary SQLite database with a single
    table, 'uploaded_data'. Returns (db_path, error_message) - exactly one
    of the two will be set.

    This is what makes the CSV upload mode work without changing anything
    about mcp_server.py or the Claude tool-use loop: once the CSV becomes a
    real SQLite file, it's served through the exact same MCP pipeline as the
    sample claims data.
    """
    import pandas as pd
    import sqlite3
    import tempfile

    try:
        df = pd.read_csv(uploaded_file)
    except Exception as e:
        return None, f"Couldn't read that as a CSV: {e}"

    if df.empty:
        return None, "That CSV has no rows."

    if len(df) > MAX_CSV_ROWS:
        return None, f"That file has {len(df):,} rows - please keep uploads under {MAX_CSV_ROWS:,} rows for this demo."

    # Sanitize column names - anything that isn't a letter, digit, or
    # underscore breaks SQL if left in a bare column name (spaces,
    # parentheses, slashes, etc. are all common in real-world CSV headers)
    import re
    df.columns = [
        re.sub(r"\W+", "_", str(c).strip()).strip("_") or f"column_{i}"
        for i, c in enumerate(df.columns)
    ]

    tmp_dir = Path(tempfile.mkdtemp())
    db_path = tmp_dir / "uploaded.db"
    conn = sqlite3.connect(db_path)
    df.to_sql("uploaded_data", conn, index=False)
    conn.close()

    return str(db_path), None


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

st.title("📊 Claims Data Assistant")
st.caption(
    "Ask questions about your data in plain English. "
    "No SQL knowledge required - Claude writes and runs the query for you via MCP."
)

api_key = get_api_key()

if "questions_asked" not in st.session_state:
    st.session_state["questions_asked"] = 0
if "csv_db_path" not in st.session_state:
    st.session_state["csv_db_path"] = None
if "csv_filename" not in st.session_state:
    st.session_state["csv_filename"] = None

data_mode = st.radio(
    "Data source",
    ["Sample healthcare claims data", "Upload your own CSV"],
    horizontal=True,
)

active_db_path = None  # None means "use the default sample claims.db"

if data_mode == "Upload your own CSV":
    uploaded_file = st.file_uploader("Upload a CSV file", type="csv")
    if uploaded_file is not None:
        if st.session_state["csv_filename"] != uploaded_file.name:
            with st.spinner("Reading your CSV and preparing it for querying..."):
                db_path, error = build_db_from_csv(uploaded_file)
            if error:
                st.error(error)
                st.session_state["csv_db_path"] = None
                st.session_state["csv_filename"] = None
            else:
                st.session_state["csv_db_path"] = db_path
                st.session_state["csv_filename"] = uploaded_file.name
                st.success(f"Loaded {uploaded_file.name} - ask away below.")

    active_db_path = st.session_state["csv_db_path"]
    if active_db_path is None:
        st.info("Upload a CSV above to start asking questions about it.")

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

if data_mode == "Sample healthcare claims data":
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
else:
    st.caption(
        "Once your CSV is loaded, ask things like \"what's the total X by Y\" "
        "or \"which Z has the highest average W\" - using your actual column names."
    )

question = st.text_input(
    "Your question",
    value=st.session_state.get("question", ""),
    placeholder="e.g. Which claim types had the highest rejection rate last quarter?",
)

def unwrap_error(e: Exception) -> Exception:
    """
    asyncio TaskGroups (used internally by the MCP client) wrap real errors
    in an ExceptionGroup - and sometimes in a chain of nested ExceptionGroups
    a few levels deep - which prints as a vague "unhandled errors in a
    TaskGroup" message. This digs down to the actual underlying error so
    it's debuggable instead of guessed at.
    """
    while hasattr(e, "exceptions") and e.exceptions:
        e = e.exceptions[0]
    return e


session_limit_hit = st.session_state["questions_asked"] >= MAX_QUESTIONS_PER_SESSION
data_not_ready = data_mode == "Upload your own CSV" and active_db_path is None
sample_db_missing = data_mode == "Sample healthcare claims data" and not DEFAULT_DB_PATH.exists()

if sample_db_missing:
    st.error(
        "The sample database (db/claims.db) wasn't found next to the app. "
        "Run `python3 db/create_db.py` from the project folder to generate it, "
        "then reload this page. (Make sure the `db/` folder - including "
        "create_db.py - was downloaded alongside app.py and mcp_server.py.)"
    )

if st.button(
    "Ask",
    type="primary",
    disabled=not api_key or session_limit_hit or data_not_ready or sample_db_missing,
):
    if not question.strip():
        st.warning("Type a question first.")
    else:
        allowed, _ = check_global_daily_limit()
        if not allowed:
            st.error(
                "This demo has hit its shared usage limit for today. "
                "Please check back tomorrow, or run it locally with your own API key."
            )
        else:
            st.session_state["questions_asked"] += 1
            with st.spinner("Claude is writing and running the query..."):
                try:
                    answer, sql = run_query(question, api_key, active_db_path)
                except Exception as e:
                    st.error(f"Something went wrong: {unwrap_error(e)}")
                else:
                    st.markdown("### Answer")
                    st.write(answer)
                    if sql:
                        with st.expander("Show generated SQL"):
                            st.code(sql, language="sql")

remaining = MAX_QUESTIONS_PER_SESSION - st.session_state["questions_asked"]
if session_limit_hit:
    st.warning(
        f"You've used your {MAX_QUESTIONS_PER_SESSION} free questions for this session. "
        "Reload the page to reset, or clone the repo to run it with your own API key."
    )
elif api_key:
    st.caption(f"{remaining} question(s) remaining in this session.")

if not api_key:
    st.info("This demo needs an Anthropic API key configured by the host to run.")
