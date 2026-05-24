"""
One-time migration script to clear stale bot memory for ALL borrowers.

Targets borrowers who have LangGraph checkpoint data, chat_sessions,
or pending scheduled_tasks from previous billing cycles.

Preserves:
- chat_history (audit trail)
- ptp records (analytics)

Safe: skips borrowers with active sessions (needs_reply/processing).
"""

import os
import sys
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set")
    sys.exit(1)


def cleanup_all_stale_memory():
    """Clear stale bot memory for all borrowers with checkpoint data."""
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            # Find all phone numbers with checkpoint/session data,
            # excluding active sessions
            cur.execute("""
                SELECT DISTINCT thread_id AS phone_number
                FROM checkpoints
                WHERE thread_id NOT IN (
                    SELECT phone_number FROM chat_sessions
                    WHERE status IN ('needs_reply', 'processing', 'human_handoff')
                )
            """)
            phones = [row["phone_number"] for row in cur.fetchall()]

            if not phones:
                print("No stale checkpoint data found. Nothing to clean.")
                return

            print(f"Found {len(phones)} borrowers with checkpoint data to clean.")

            # Show counts before cleanup
            cur.execute("SELECT COUNT(*) AS cnt FROM checkpoints")
            print(f"  Checkpoints before: {cur.fetchone()['cnt']}")
            cur.execute("SELECT COUNT(*) AS cnt FROM chat_sessions")
            print(f"  Chat sessions before: {cur.fetchone()['cnt']}")

            # Batch delete chat_sessions
            cur.execute("DELETE FROM chat_sessions WHERE phone_number = ANY(%s)", (phones,))
            sessions_deleted = cur.rowcount
            print(f"  Deleted {sessions_deleted} chat_sessions")

            # Batch delete scheduled_tasks
            cur.execute("DELETE FROM scheduled_tasks WHERE phone_number = ANY(%s)", (phones,))
            tasks_deleted = cur.rowcount
            print(f"  Deleted {tasks_deleted} scheduled_tasks")

            # Batch delete LangGraph checkpoint tables
            for table in ["checkpoint_blobs", "checkpoint_writes", "checkpoints"]:
                try:
                    cur.execute(f"DELETE FROM {table} WHERE thread_id = ANY(%s)", (phones,))
                    print(f"  Deleted {cur.rowcount} rows from {table}")
                except Exception as e:
                    print(f"  Warning: could not clean {table}: {e}")

            conn.commit()
            print(f"\nDone. Cleaned memory for {len(phones)} borrowers.")

            # Verify
            cur.execute("SELECT COUNT(*) AS cnt FROM checkpoints")
            print(f"  Checkpoints after: {cur.fetchone()['cnt']}")
            cur.execute("SELECT COUNT(*) AS cnt FROM chat_history")
            print(f"  Chat history preserved: {cur.fetchone()['cnt']} messages")
            cur.execute("SELECT COUNT(*) AS cnt FROM ptp")
            print(f"  PTP records preserved: {cur.fetchone()['cnt']} records")


if __name__ == "__main__":
    print("=== One-time stale memory cleanup ===")
    print("This will clear LangGraph checkpoints, chat_sessions, and scheduled_tasks")
    print("for all borrowers with stale data. chat_history and ptp are preserved.\n")

    confirm = input("Proceed? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("Aborted.")
        sys.exit(0)

    cleanup_all_stale_memory()
