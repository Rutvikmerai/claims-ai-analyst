"""
mcp_server.py

This is a real MCP (Model Context Protocol) server. It does not call Claude -
it only exposes a SQLite database to any MCP client (Claude Desktop, our
Streamlit app, or any other MCP-compatible client) through three standard
primitives:

  - Resource: "schema://claims_db"  -> lets a client read the DB schema
  - Tool:     "run_sql_query"       -> executes a read-only SQL query
  - Tool:     "list_tables"         -> lists available tables

Keeping this as a standalone server (rather than baking SQL logic into the
Streamlit app) is the whole point of using MCP: the database access layer is
decoupled from whatever AI client talks to it. You could point Claude
Desktop at this exact same server file with zero changes.

By default this serves db/claims.db (the sample healthcare claims data). It
also accepts an optional command-line argument pointing at a different
SQLite file - this is how the app's "upload your own CSV" mode works: the
uploaded CSV gets converted to a temporary SQLite database, and this same
server file is launched pointed at that file instead, with no code changes
needed. The MCP layer doesn't care what data it's serving - that's the point.
"""

import sys
import sqlite3
from pathlib import Path

from mcp.server.fastmcp import FastMCP

DEFAULT_DB_PATH = Path(__file__).parent / "db" / "claims.db"

# If a path is passed as the first CLI argument, serve that database instead
# of the default sample claims data. Used by app.py's CSV upload mode.
DB_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB_PATH

mcp = FastMCP("claims-db-server")


def _get_connection():
    # check_same_thread=False is safe here because we open/close per call
    return sqlite3.connect(DB_PATH, check_same_thread=False)


@mcp.resource("schema://claims_db")
def get_schema() -> str:
    """
    Exposes the database schema as an MCP Resource so the AI client can
    read it once and understand table/column structure before writing SQL.
    """
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute("SELECT sql FROM sqlite_master WHERE type='table'")
    schema_statements = [row[0] for row in cur.fetchall() if row[0]]
    conn.close()
    return "\n\n".join(schema_statements)


@mcp.tool()
def list_tables() -> list[str]:
    """Returns the list of tables available in the claims database."""
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cur.fetchall()]
    conn.close()
    return tables


@mcp.tool()
def run_sql_query(query: str) -> str:
    """
    Executes a read-only SQL query against the healthcare claims database
    and returns the results as a formatted string.

    Only SELECT statements are permitted. This is a hard safety boundary:
    the tool refuses anything that could mutate data (INSERT/UPDATE/DELETE/
    DROP/ALTER), which matters a lot once you let an LLM write the SQL
    itself instead of a human reviewing every query.

    Args:
        query: A single SQL SELECT statement.
    """
    normalized = query.strip().lower()

    if not normalized.startswith("select"):
        return "ERROR: Only SELECT statements are allowed. Query rejected."

    forbidden = ["insert", "update", "delete", "drop", "alter", "create", "attach", "pragma"]
    if any(keyword in normalized for keyword in forbidden):
        return "ERROR: Query contains a disallowed keyword. Only read-only SELECT queries are permitted."

    conn = _get_connection()
    cur = conn.cursor()
    try:
        cur.execute(query)
        columns = [desc[0] for desc in cur.description] if cur.description else []
        rows = cur.fetchmany(200)  # cap result size returned to the LLM
    except sqlite3.Error as e:
        conn.close()
        return f"SQL ERROR: {e}"
    conn.close()

    if not rows:
        return "Query ran successfully but returned no rows."

    header = " | ".join(columns)
    separator = "-" * len(header)
    body = "\n".join(" | ".join(str(v) for v in row) for row in rows)
    return f"{header}\n{separator}\n{body}"


if __name__ == "__main__":
    # Runs the server over stdio - this is the transport the Streamlit
    # app's MCP client will connect to as a subprocess.
    mcp.run()
