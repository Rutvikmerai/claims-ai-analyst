# Claims Data Assistant - Natural Language Querying for Healthcare Claims

## Problem

Business stakeholders — claims managers, compliance leads, finance — routinely need quick answers from claims data: *"Which claim types are getting rejected most?" "What did we pay out on pharmacy claims this year?"* Today, that means filing a request and waiting on an analyst to write, run, and interpret a SQL query. For simple, recurring questions, this creates hours of turnaround time and pulls analysts away from higher-value work.

## Approach

I built a natural-language interface where a stakeholder types a question in plain English and gets a direct, human-readable answer — backed by a real query against the claims database, not a guess.

The system uses the **Model Context Protocol (MCP)** to cleanly separate the database access layer from the AI layer:

- An **MCP server** exposes the claims database as a set of standardized primitives: a **Resource** (the live schema) and **Tools** (`run_sql_query`, `list_tables`). It enforces a hard safety boundary — only read-only `SELECT` statements are permitted; anything else is rejected before it reaches SQLite.
- **Claude (Anthropic API)** reads the schema, interprets the user's question, and writes the SQL needed to answer it, calling the MCP tool to execute it.
- A **Streamlit** front end handles the conversation and displays the answer, with the generated SQL available on demand for transparency.

Because the database logic lives entirely in the MCP server, it's portable — the same server could be pointed at from Claude Desktop or any other MCP client with zero code changes.

## Impact

- Turns ad-hoc data requests that took hours of analyst time into answers delivered in seconds.
- Removes the SQL literacy requirement for non-technical stakeholders to self-serve on common claims questions.
- Keeps a human-readable audit trail: every answer is backed by an inspectable, read-only SQL query.

## Tech Stack

- **Python** — application logic
- **SQLite** — claims database (providers, members, claims — ~3,000 synthetic claims records)
- **Model Context Protocol (MCP)** — decouples database access from the AI client via Tools and Resources
- **Claude API** (`claude-sonnet-4-6`) — natural language understanding, SQL generation, and answer summarization
- **Streamlit** — user interface

## Architecture

```
 ┌─────────────┐        question         ┌───────────────┐
 │  Streamlit  │ ─────────────────────►  │   Claude API   │
 │     UI      │                          │ (tool-use loop)│
 └─────────────┘                          └───────┬────────┘
        ▲                                          │ calls tool: run_sql_query
        │ final answer + SQL                       ▼
        │                                  ┌────────────────┐
        └───────────────────────────────── │   MCP Server    │
                                            │ (stdio transport)│
                                            └────────┬─────────┘
                                                     │ SELECT ...
                                                     ▼
                                            ┌────────────────┐
                                            │  SQLite DB      │
                                            │  (claims.db)    │
                                            └────────────────┘
```

## How to Run

```bash
# 1. Clone and install dependencies
git clone <your-repo-url>
cd claims-ai-analyst
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

# 2. Build the sample database
python3 db/create_db.py

# 3. Set your Anthropic API key
export ANTHROPIC_API_KEY=your-api-key-here   # or paste it into the app's UI field

# 4. Run the app
streamlit run app.py
```

Then open the local URL Streamlit prints, and try a question like:

> "Which claim types had the highest rejection rate?"

## Example Questions

- Which claim types had the highest rejection rate?
- What is the average billed amount for inpatient claims?
- Which provider specialty has submitted the most claims?
- What are the top 3 rejection reasons this year?
- How much total revenue was paid out for pharmacy claims?

## Harder / Edge-Case Questions (for demonstrating boundaries)

These aren't in the happy-path demo set on purpose - they're there to show the assistant handles imperfect questions gracefully instead of breaking. Good to run live if an interviewer wants to push on the tool:

- **"Which provider should we be worried about?"** — Ambiguous by design; there's no `risk` column. Claude has to decide what "worried about" means from the schema it has, and reasonably interprets it as rejection rate by provider, filtering out low-volume providers so the answer isn't skewed by a provider with only a handful of claims. (Verified: Provider Group 4, Orthopedics, comes back at a 19% rejection rate over 116 claims — a real, defensible number, not a hallucination.)
- **"What's driving the pharmacy rejections?"** — Partially answerable. The schema has `rejection_reason` but no true root-cause data, so Claude can break down rejection reasons by count (e.g., "Duplicate claim" is the top reason at 26 occurrences) but can't explain underlying causes beyond that. Good to narrate in an interview: "the assistant answers what the data supports and doesn't overreach into causal claims it can't back up."
- **"Update claim 105's status to Paid."** — An out-of-scope write request. The MCP server's `run_sql_query` tool rejects anything that isn't a `SELECT` before it reaches SQLite, so this fails safely with a clear message rather than silently doing nothing or erroring unpredictably. This is the best moment to demo the safety design if someone asks "what stops the AI from messing up the database."

## Notes on Design Choices

- **Read-only enforcement happens at the MCP tool level, not the prompt level.** Prompt instructions telling Claude to "only write SELECT queries" are a suggestion; the server-side keyword check is the actual safety boundary. This matters as soon as an LLM is writing SQL against a real database.
- **The schema is read live from the database via an MCP Resource**, not hardcoded into the prompt. If the schema changes, the assistant adapts automatically.
- **Result sets are capped at 200 rows** before being returned to Claude, to keep token usage predictable and avoid flooding the model with raw data it doesn't need to summarize an answer.
