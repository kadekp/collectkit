"""
PostgreSQL data layer for the worker.
Shares a PostgreSQL instance with the receiver service.
"""

import os
import time
from typing import Optional
from datetime import datetime
from .timezone_utils import today_local
from .i18n import format_currency
from contextlib import contextmanager
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from pydantic import BaseModel
from dotenv import load_dotenv

from .logging_config import get_logger

load_dotenv()

logger = get_logger(__name__)

# A/B Test control group labels (must match worker.py)
_control_labels_raw = os.getenv("CONTROL_GROUP_LABELS", "")
CONTROL_GROUP_LABELS = frozenset(
    label.strip() for label in _control_labels_raw.split(",") if label.strip()
)

# Retry configuration
MAX_RETRIES = 5
INITIAL_RETRY_DELAY = 1  # seconds


# ============================================================================
# MODELS (same as database.py)
# ============================================================================

class Loan(BaseModel):
    """Historical loan record"""
    loan_amount: float
    loan_date: str
    loan_admin_fee: float = 0


class BorrowerDetails(BaseModel):
    """Complete borrower information"""
    phone_number: str
    customer_number: str
    customer_name: str
    loans: list[Loan]
    due_date: str
    days_late: int
    billing_amount: float
    status: str
    label: Optional[str] = None
    registration_date: str  # YYYY-MM-DD format


class Ptp(BaseModel):
    """Promise to Pay record"""
    id: int
    phone_number: str
    promise_amount: float
    promise_date: str
    status: str
    created_at: str


# ============================================================================
# CONNECTION
# ============================================================================

def get_database_url() -> str:
    """Get DATABASE_URL from environment."""
    url = os.getenv("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL environment variable not set")
    return url


@contextmanager
def get_connection():
    """
    Get PostgreSQL database connection with context manager.
    Includes retry logic with exponential backoff for transient failures.
    """
    conn = None
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            conn = psycopg.connect(get_database_url(), row_factory=dict_row)
            break
        except psycopg.OperationalError as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                delay = INITIAL_RETRY_DELAY * (2 ** attempt)
                logger.warning("DB connection failed, retrying", extra={
                    "event": "db_connection_retry",
                    "attempt": attempt + 1,
                    "max_retries": MAX_RETRIES,
                    "delay": delay,
                    "error": str(e),
                })
                time.sleep(delay)
            else:
                logger.error("DB connection failed permanently", extra={
                    "event": "db_connection_failed",
                    "attempts": MAX_RETRIES,
                    "error": str(e),
                })
                raise

    if conn is None:
        raise last_error or psycopg.OperationalError("Failed to connect to database")

    try:
        yield conn
    finally:
        conn.close()


# ============================================================================
# CHAT SESSION MANAGEMENT
# ============================================================================

def get_sessions_needing_reply(debounce_seconds: int = 4) -> list[dict]:
    """
    Get chat sessions that need a reply.
    Only returns sessions where:
    - status = 'needs_reply'
    - last_message_at is older than debounce_seconds ago (user stopped typing)
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT phone_number, last_message_at
                FROM chat_sessions
                WHERE status = 'needs_reply'
                  AND last_message_at < NOW() - INTERVAL '%s seconds'
            """, (debounce_seconds,))
            return cur.fetchall()


def lock_session(phone_number: str) -> bool:
    """
    Lock a session for processing (atomic operation).
    Returns True if lock was acquired, False if already locked/processing.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE chat_sessions
                SET status = 'processing'
                WHERE phone_number = %s AND status = 'needs_reply'
                RETURNING phone_number
            """, (phone_number,))
            result = cur.fetchone()
            conn.commit()
            return result is not None


def unlock_session(phone_number: str) -> None:
    """Unlock a session after processing (set status back to idle).

    Only sets to 'idle' if still 'processing'.
    This preserves 'human_handoff' status if set by the agent.
    Also preserves 'needs_reply' status if set by retry logic.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE chat_sessions
                SET status = 'idle'
                WHERE phone_number = %s AND status = 'processing'
            """, (phone_number,))
            conn.commit()


def set_session_needs_retry(phone_number: str) -> int:
    """Set session back to needs_reply for retry and increment error_count.

    Returns the new error_count value. Only updates if status is 'processing',
    so unlock_session() in the finally block becomes a no-op (same pattern
    as human_handoff).
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE chat_sessions
                SET status = 'needs_reply',
                    error_count = COALESCE(error_count, 0) + 1
                WHERE phone_number = %s AND status = 'processing'
                RETURNING error_count
            """, (phone_number,))
            result = cur.fetchone()
            conn.commit()
            return result["error_count"] if result else 0


def reset_error_count(phone_number: str) -> None:
    """Reset error_count to 0 after successful processing."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE chat_sessions
                SET error_count = 0
                WHERE phone_number = %s AND error_count > 0
            """, (phone_number,))
            conn.commit()


# ============================================================================
# HUMAN HANDOFF MANAGEMENT
# ============================================================================

def set_human_handoff(phone_number: str, reason: str) -> bool:
    """
    Set a session to human handoff mode.
    The bot will stop auto-replying until handoff is disabled.
    
    Args:
        phone_number: User's phone number
        reason: Reason for handoff (logged for reference)
    
    Returns:
        True if successful, False otherwise
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Update session status to human_handoff
            cur.execute("""
                UPDATE chat_sessions
                SET status = 'human_handoff'
                WHERE phone_number = %s
                RETURNING phone_number
            """, (phone_number,))
            result = cur.fetchone()
            
            # Log the handoff reason in chat_history as system message
            if result:
                cur.execute("""
                    INSERT INTO chat_history (phone_number, sender, message_content, is_processed, created_at)
                    VALUES (%s, 'system', %s, TRUE, NOW())
                """, (phone_number, f"[HANDOFF] Reason: {reason}"))
            
            conn.commit()
            return result is not None


def disable_human_handoff(phone_number: str) -> bool:
    """
    Disable human handoff and return session to bot control.
    Sets status back to 'idle' so bot can respond to new messages.
    
    Returns:
        True if successful, False otherwise
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE chat_sessions
                SET status = 'idle'
                WHERE phone_number = %s AND status = 'human_handoff'
                RETURNING phone_number
            """, (phone_number,))
            result = cur.fetchone()
            
            if result:
                # Mark messages accumulated during handoff as processed
                cur.execute("""
                    UPDATE chat_history
                    SET is_processed = TRUE
                    WHERE phone_number = %s
                      AND sender = 'user'
                      AND is_processed = FALSE
                """, (phone_number,))

                cur.execute("""
                    INSERT INTO chat_history (phone_number, sender, message_content, is_processed, created_at)
                    VALUES (%s, 'system', '[HANDOFF ENDED] Bot resumed', TRUE, NOW())
                """, (phone_number,))

            conn.commit()
            return result is not None


def get_handoff_sessions() -> list[dict]:
    """Get all sessions currently in human handoff mode."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT phone_number, last_message_at
                FROM chat_sessions
                WHERE status = 'human_handoff'
                ORDER BY last_message_at DESC
            """)
            return cur.fetchall()


# ============================================================================
# CHAT HISTORY MANAGEMENT
# ============================================================================

def get_unprocessed_messages(phone_number: str) -> list[dict]:
    """
    Get all unprocessed user messages for a phone number.
    Returns messages in chronological order for aggregation.
    Excludes WhatsApp Business auto-replies.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, message_content, created_at
                FROM chat_history
                WHERE phone_number = %s
                  AND sender = 'user'
                  AND is_processed = FALSE
                  AND is_auto_reply = FALSE
                ORDER BY created_at
            """, (phone_number,))
            return cur.fetchall()


def get_unprocessed_messages_with_images(phone_number: str) -> list[dict]:
    """
    Get all unprocessed user messages for a phone number, including image data.
    Returns messages in chronological order for aggregation.
    Excludes WhatsApp Business auto-replies.

    Returns:
        List of dicts with keys: id, message_content, image_data, created_at
        image_data is base64-encoded image or None for text messages
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, message_content, image_data, created_at
                FROM chat_history
                WHERE phone_number = %s
                  AND sender = 'user'
                  AND is_processed = FALSE
                  AND is_auto_reply = FALSE
                ORDER BY created_at
            """, (phone_number,))
            return cur.fetchall()


def mark_messages_processed(phone_number: str, message_ids: list[int] | None = None) -> None:
    """Mark user messages as processed.

    Args:
        phone_number: User's phone number (used as fallback when no IDs provided)
        message_ids: Specific message IDs to mark. When provided, only these
            messages are marked — preventing race conditions where new messages
            arrive during processing and get incorrectly swept up.
            When None, falls back to marking all unprocessed messages for the
            phone number (used for non-borrower skip path).
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            if message_ids:
                cur.execute("""
                    UPDATE chat_history
                    SET is_processed = TRUE
                    WHERE id = ANY(%s)
                      AND is_processed = FALSE
                """, (message_ids,))
            else:
                cur.execute("""
                    UPDATE chat_history
                    SET is_processed = TRUE
                    WHERE phone_number = %s
                      AND sender = 'user'
                      AND is_processed = FALSE
                """, (phone_number,))
            conn.commit()


def save_bot_response(phone_number: str, message: str, sender: str = 'bot') -> None:
    """Save bot response to chat_history.

    Args:
        phone_number: User's phone number
        message: Message content to save
        sender: Sender identifier (default 'bot'). Use:
            - 'bot' for AI chatbot interactive responses
            - 'bot-task' for scheduled task confirmations
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO chat_history (phone_number, sender, message_content, created_at)
                VALUES (%s, %s, %s, NOW())
            """, (phone_number, sender, message))
            conn.commit()


# ============================================================================
# BUSINESS DATA (adapted from database.py for PostgreSQL)
# ============================================================================

def fetch_borrower_data(phone_number: str) -> Optional[BorrowerDetails]:
    """
    Fetch borrower data from database.
    Returns BorrowerDetails if found, None otherwise.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Get borrower
            cur.execute("""
                SELECT * FROM borrowers WHERE phone_number = %s
            """, (phone_number,))
            borrower_row = cur.fetchone()

            if not borrower_row:
                return None

            # Get all loans for this borrower
            cur.execute("""
                SELECT loan_amount, loan_date, loan_admin_fee
                FROM loans
                WHERE phone_number = %s
                ORDER BY loan_date
            """, (phone_number,))
            loans_rows = cur.fetchall()

    # Convert to Pydantic models
    loans = [
        Loan(
            loan_amount=row["loan_amount"],
            loan_date=row["loan_date"],
            loan_admin_fee=row["loan_admin_fee"] or 0
        )
        for row in loans_rows
    ]

    borrower = BorrowerDetails(
        phone_number=borrower_row["phone_number"],
        customer_number=borrower_row["customer_number"],
        customer_name=borrower_row["customer_name"],
        loans=loans,
        due_date=borrower_row["due_date"],
        days_late=borrower_row["days_late"],
        billing_amount=borrower_row["billing_amount"],
        status=borrower_row["status"],
        label=borrower_row.get("label"),
        registration_date=borrower_row["registration_date"],
    )

    return borrower


def is_registered_borrower(phone_number: str) -> bool:
    """
    Check if a phone number exists in the borrowers table.
    Quick check without fetching full borrower details.
    
    Returns:
        True if borrower exists, False otherwise.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM borrowers WHERE phone_number = %s LIMIT 1
            """, (phone_number,))
            return cur.fetchone() is not None


# ── Bot exclusion list (cached) ──────────────────────────────────

_excluded_numbers: frozenset = frozenset()
_excluded_numbers_loaded_at: float = 0
_EXCLUSION_CACHE_TTL = 300  # 5 minutes


def load_excluded_numbers() -> frozenset:
    """Load all excluded phone numbers from bot_exclusions table."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT phone_number FROM bot_exclusions")
            rows = cur.fetchall()
            return frozenset(row["phone_number"] for row in rows)


def is_excluded_number(phone_number: str) -> bool:
    """
    Check if a phone number is in the bot exclusion list.
    Uses an in-memory cache refreshed every 5 minutes.
    """
    global _excluded_numbers, _excluded_numbers_loaded_at

    now = time.time()
    if now - _excluded_numbers_loaded_at > _EXCLUSION_CACHE_TTL:
        _excluded_numbers = load_excluded_numbers()
        _excluded_numbers_loaded_at = now
        logger.info(
            "Exclusion list refreshed",
            extra={"event": "exclusion_list_refreshed",
                   "count": len(_excluded_numbers)},
        )

    return phone_number in _excluded_numbers


def get_max_ptp_days(days_late: int) -> int:
    """
    Get maximum PTP days allowed based on DPD (Days Past Due).
    
    Business logic:
    - DPD ≤ 14 days: 14 days max (early stage, more flexibility)
    - DPD 15-30 days: 10 days max (mid stage, tighter window)
    - DPD 31+ days: 7 days max (late stage, maximum urgency)
    """
    if days_late <= 14:
        return 14
    elif days_late <= 30:
        return 10
    else:
        return 7


def record_ptp(phone_number: str, promise_amount: float, promise_date: str) -> str:
    """
    Record a Promise to Pay (PTP) in the database.
    """
    # Validate promise amount
    if promise_amount <= 0:
        return "✗ Promise amount must be greater than zero"

    # Validate promise date format and not in past
    try:
        pdate = datetime.strptime(promise_date, "%Y-%m-%d").date()
        if pdate < today_local():
            return "✗ Promise date cannot be in the past"
    except ValueError:
        return "✗ Invalid date format. Use YYYY-MM-DD"

    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                # Check borrower status before recording PTP
                cur.execute("""
                    SELECT status, billing_amount, customer_name, days_late
                    FROM borrowers
                    WHERE phone_number = %s
                """, (phone_number,))
                borrower = cur.fetchone()

                if not borrower:
                    return "✗ Phone number is not registered"

                if borrower["status"] == "PAID":
                    return "✗ The bill for this period is already paid; no promise needed"

                if borrower["billing_amount"] <= 0:
                    return "✗ No outstanding balance to pay"

                if promise_amount > borrower["billing_amount"]:
                    return (
                        f"✗ Promise amount {format_currency(promise_amount)} exceeds "
                        f"the outstanding balance {format_currency(borrower['billing_amount'])}"
                    )

                # Validate PTP date is within allowed range based on DPD
                days_late = borrower["days_late"]
                max_ptp_days = get_max_ptp_days(days_late)
                days_from_today = (pdate - today_local()).days
                if days_from_today > max_ptp_days:
                    return f"✗ Promise date may be at most {max_ptp_days} day(s) from today"

                # Supersede existing pending PTPs before creating new one
                cur.execute("""
                    UPDATE ptp SET status = 'SUPERSEDED'
                    WHERE phone_number = %s AND status = 'PENDING'
                """, (phone_number,))
                superseded_count = cur.rowcount

                # Insert PTP record
                cur.execute("""
                    INSERT INTO ptp (phone_number, promise_amount, promise_date, status)
                    VALUES (%s, %s, %s, 'PENDING')
                    RETURNING id
                """, (phone_number, promise_amount, promise_date))

                result = cur.fetchone()
                ptp_id = result["id"]
                name = borrower["customer_name"]
                conn.commit()

                if superseded_count > 0:
                    return f"✓ PTP updated for {name}: {format_currency(promise_amount)} on {promise_date} (replaces previous promise)"
                return f"✓ PTP recorded for {name}: {format_currency(promise_amount)} on {promise_date} (ID: {ptp_id})"

            except Exception as e:
                return f"✗ Error recording PTP: {str(e)}"


def get_ptp_history(phone_number: str) -> list[Ptp]:
    """
    Fetch active (PENDING) Promise to Pay records for a borrower.
    Only shows pending PTPs - fulfilled/missed/superseded are filtered out.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, phone_number, promise_amount, promise_date, status, created_at
                FROM ptp
                WHERE phone_number = %s AND status = 'PENDING'
                ORDER BY created_at DESC
            """, (phone_number,))
            ptp_rows = cur.fetchall()

    ptps = [
        Ptp(
            id=row["id"],
            phone_number=row["phone_number"],
            promise_amount=row["promise_amount"],
            promise_date=str(row["promise_date"]),  # Convert DATE to string
            status=row["status"],
            created_at=str(row["created_at"]),
        )
        for row in ptp_rows
    ]

    return ptps


# ============================================================================
# RESET / CLEAR DATA
# ============================================================================

def clear_chat_history(phone_number: str) -> int:
    """Clear all chat history for a phone number. Returns count of deleted messages."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM chat_history
                WHERE phone_number = %s
            """, (phone_number,))
            deleted_count = cur.rowcount
            conn.commit()
            return deleted_count


def clear_chat_session(phone_number: str) -> None:
    """Clear chat session for a phone number."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM chat_sessions
                WHERE phone_number = %s
            """, (phone_number,))
            conn.commit()


def clear_langgraph_checkpoints(phone_number: str) -> None:
    """Clear LangGraph checkpoint data for a phone number (thread_id)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            # LangGraph PostgresSaver uses thread_id as the key
            # Tables created by PostgresSaver.setup()
            tables = ['checkpoint_blobs', 'checkpoint_writes', 'checkpoints']
            for table in tables:
                try:
                    cur.execute(f"""
                        DELETE FROM {table}
                        WHERE thread_id = %s
                    """, (phone_number,))
                except Exception:
                    pass  # Table may not exist or have different schema
            conn.commit()


def clear_ptp_records(phone_number: str) -> int:
    """Clear all PTP records for a phone number. Returns count of deleted records."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM ptp
                WHERE phone_number = %s
            """, (phone_number,))
            deleted_count = cur.rowcount
            conn.commit()
            return deleted_count


def clear_scheduled_tasks(phone_number: str) -> int:
    """Clear all scheduled tasks for a phone number. Returns count of deleted tasks."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM scheduled_tasks
                WHERE phone_number = %s
            """, (phone_number,))
            deleted_count = cur.rowcount
            conn.commit()
            return deleted_count


def cleanup_paid_borrower_memory() -> dict:
    """Clear bot memory for PAID (paid) borrowers.

    Removes LangGraph checkpoints, chat_sessions, and scheduled_tasks
    so the bot starts fresh if the borrower gets a new billing cycle.
    Preserves chat_history (audit trail) and ptp (analytics).

    Skips borrowers with active sessions (needs_reply/processing) for safety.

    Returns:
        Dict with counts: chat_sessions, scheduled_tasks, checkpoints
    """
    result = {"chat_sessions": 0, "scheduled_tasks": 0, "checkpoints": 0}

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Get PAID phone numbers, excluding active sessions
            cur.execute("""
                SELECT b.phone_number
                FROM borrowers b
                WHERE b.status = 'PAID'
                  AND b.phone_number NOT IN (
                      SELECT cs.phone_number FROM chat_sessions cs
                      WHERE cs.status IN ('needs_reply', 'processing', 'human_handoff')
                  )
                  AND (
                      EXISTS (SELECT 1 FROM chat_sessions cs WHERE cs.phone_number = b.phone_number)
                      OR EXISTS (SELECT 1 FROM checkpoints cp WHERE cp.thread_id = b.phone_number)
                      OR EXISTS (SELECT 1 FROM scheduled_tasks st WHERE st.phone_number = b.phone_number AND st.status = 'pending')
                  )
            """)
            phones = [row["phone_number"] for row in cur.fetchall()]

            if not phones:
                return result

            # Mark any unprocessed messages as processed before cleanup
            cur.execute("""
                UPDATE chat_history
                SET is_processed = TRUE
                WHERE phone_number = ANY(%s)
                  AND sender = 'user'
                  AND is_processed = FALSE
            """, (phones,))

            # Batch delete chat_sessions
            cur.execute("DELETE FROM chat_sessions WHERE phone_number = ANY(%s)", (phones,))
            result["chat_sessions"] = cur.rowcount

            # Batch delete scheduled_tasks
            cur.execute("DELETE FROM scheduled_tasks WHERE phone_number = ANY(%s)", (phones,))
            result["scheduled_tasks"] = cur.rowcount

            # Batch delete LangGraph checkpoint tables
            checkpoint_deleted = 0
            for table in ["checkpoint_blobs", "checkpoint_writes", "checkpoints"]:
                try:
                    cur.execute(
                        sql.SQL("DELETE FROM {tbl} WHERE thread_id = ANY(%s)").format(
                            tbl=sql.Identifier(table)
                        ),
                        (phones,),
                    )
                    checkpoint_deleted += cur.rowcount
                except Exception:
                    pass  # Table may not exist
            result["checkpoints"] = checkpoint_deleted

            conn.commit()
            logger.info("Paid borrower memory cleanup", extra={
                "event": "paid_borrower_cleanup",
                "borrowers_cleaned": len(phones),
                **result,
            })

    return result


# ============================================================================
# SCHEDULED TASKS MANAGEMENT
# ============================================================================

class ScheduledTask(BaseModel):
    """Scheduled task record"""
    id: int
    phone_number: str
    customer_number: str
    task_type: str
    scheduled_at: datetime
    status: str
    result: Optional[str]
    created_at: datetime


def create_scheduled_task(
    phone_number: str,
    customer_number: str,
    task_type: str = "payment_check",
    delay_hours: float = 1.0
) -> int:
    """
    Create a scheduled task to be executed after delay_hours.
    
    Args:
        phone_number: User's phone number
        customer_number: Customer ID for payment verification
        task_type: Type of task (default: 'payment_check')
        delay_hours: Hours to wait before executing (default: 1)
    
    Returns:
        The task ID
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Use interval multiplication to avoid string interpolation issues
            cur.execute("""
                INSERT INTO scheduled_tasks (phone_number, customer_number, task_type, scheduled_at)
                VALUES (%s, %s, %s, NOW() + (%s * INTERVAL '1 hour'))
                RETURNING id
            """, (phone_number, customer_number, task_type, delay_hours))
            result = cur.fetchone()
            conn.commit()
            task_id = result["id"]
            logger.info(
                "Created scheduled task",
                extra={
                    "event": "scheduled_task_created",
                    "task_id": task_id,
                    "task_type": task_type,
                    "phone_number": phone_number,
                    "delay_hours": delay_hours,
                },
            )
            return task_id


def get_due_scheduled_tasks() -> list[ScheduledTask]:
    """
    Get all scheduled tasks that are due for execution.
    Returns tasks where status='pending' AND scheduled_at <= NOW()
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, phone_number, customer_number, task_type, 
                       scheduled_at, status, result, created_at
                FROM scheduled_tasks
                WHERE status = 'pending' AND scheduled_at <= NOW()
                ORDER BY scheduled_at
            """)
            rows = cur.fetchall()
    
    return [
        ScheduledTask(
            id=row["id"],
            phone_number=row["phone_number"],
            customer_number=row["customer_number"],
            task_type=row["task_type"],
            scheduled_at=row["scheduled_at"],
            status=row["status"],
            result=row["result"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


def mark_task_completed(task_id: int, result: str) -> bool:
    """
    Mark a scheduled task as completed.
    
    Args:
        task_id: The task ID
        result: Result message/status to store
    
    Returns:
        True if successful, False otherwise
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE scheduled_tasks
                SET status = 'completed', result = %s
                WHERE id = %s
                RETURNING id
            """, (result, task_id))
            result_row = cur.fetchone()
            conn.commit()
            return result_row is not None


def mark_task_failed(task_id: int, error_message: str) -> bool:
    """
    Mark a scheduled task as failed.
    
    Args:
        task_id: The task ID
        error_message: Error message to store
    
    Returns:
        True if successful, False otherwise
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE scheduled_tasks
                SET status = 'failed', result = %s
                WHERE id = %s
                RETURNING id
            """, (error_message, task_id))
            result_row = cur.fetchone()
            conn.commit()
            return result_row is not None


def cancel_pending_tasks(phone_number: str, task_type: str = "payment_check") -> int:
    """
    Cancel all pending tasks for a user.
    Useful if user pays before the scheduled check.
    
    Args:
        phone_number: User's phone number
        task_type: Type of task to cancel
    
    Returns:
        Number of tasks cancelled
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE scheduled_tasks
                SET status = 'cancelled', result = 'Cancelled by system'
                WHERE phone_number = %s AND task_type = %s AND status = 'pending'
            """, (phone_number, task_type))
            cancelled_count = cur.rowcount
            conn.commit()
            if cancelled_count > 0:
                logger.info(
                    "Cancelled pending tasks",
                    extra={
                        "event": "scheduled_tasks_cancelled",
                        "cancelled_count": cancelled_count,
                        "task_type": task_type,
                        "phone_number": phone_number,
                    },
                )
            return cancelled_count


def has_pending_payment_check(phone_number: str) -> bool:
    """
    Check if user already has a pending payment check scheduled.
    Used to prevent duplicate scheduled tasks.
    
    Args:
        phone_number: User's phone number
    
    Returns:
        True if there's a pending payment_check task
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM scheduled_tasks
                WHERE phone_number = %s 
                  AND task_type = 'payment_check' 
                  AND status = 'pending'
                LIMIT 1
            """, (phone_number,))
            return cur.fetchone() is not None


def get_borrowers_with_outstanding_bills() -> list[dict]:
    """
    Get all borrowers who have unpaid bills.
    Used for bulk payment status checking.
    
    Returns:
        List of dicts with phone_number, customer_number, customer_name
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT phone_number, customer_number, customer_name
                FROM borrowers
                WHERE billing_amount > 0 AND status != 'PAID'
                ORDER BY phone_number
            """)
            return cur.fetchall()


def get_borrowers_for_payment_check(priority: str = "all") -> list[dict]:
    """
    Get borrowers for payment status checking with priority filtering.
    
    Priority levels:
    - "high": Only borrowers with pending PTP OR chatted in last 7 days
    - "all": All borrowers with outstanding bills
    
    Args:
        priority: "high" for high-priority only, "all" for all borrowers
    
    Returns:
        List of dicts with phone_number, customer_number, customer_name
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            if priority == "high":
                # High priority: pending PTP or recent chat activity (last 7 days)
                cur.execute("""
                    SELECT DISTINCT b.phone_number, b.customer_number, b.customer_name
                    FROM borrowers b
                    LEFT JOIN chat_sessions cs ON b.phone_number = cs.phone_number
                    LEFT JOIN ptp p ON b.phone_number = p.phone_number AND p.status = 'PENDING'
                    WHERE b.billing_amount > 0 
                      AND b.status != 'PAID'
                      AND (
                        cs.last_message_at >= NOW() - INTERVAL '7 days'
                        OR p.id IS NOT NULL
                      )
                    ORDER BY b.phone_number
                """)
            else:
                # All borrowers with outstanding bills
                cur.execute("""
                    SELECT phone_number, customer_number, customer_name
                    FROM borrowers
                    WHERE billing_amount > 0 AND status != 'PAID'
                    ORDER BY phone_number
                """)
            return cur.fetchall()


def get_pending_scheduled_task_for_borrower(phone_number: str) -> Optional[ScheduledTask]:
    """
    Get pending payment_check task for a specific borrower.
    Used during bulk payment check to determine if message should be sent.
    
    Args:
        phone_number: User's phone number
    
    Returns:
        ScheduledTask if exists, None otherwise
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, phone_number, customer_number, task_type,
                       scheduled_at, status, result, created_at
                FROM scheduled_tasks
                WHERE phone_number = %s 
                  AND task_type = 'payment_check' 
                  AND status = 'pending'
                LIMIT 1
            """, (phone_number,))
            row = cur.fetchone()
    
    if row is None:
        return None
    
    return ScheduledTask(
        id=row["id"],
        phone_number=row["phone_number"],
        customer_number=row["customer_number"],
        task_type=row["task_type"],
        scheduled_at=row["scheduled_at"],
        status=row["status"],
        result=row["result"],
        created_at=row["created_at"],
    )


# ============================================================================
# PTP LIFECYCLE MANAGEMENT
# ============================================================================

def mark_ptps_fulfilled(phone_number: str) -> int:
    """
    Mark all pending PTPs as fulfilled when payment is confirmed.
    
    Args:
        phone_number: User's phone number
    
    Returns:
        Number of PTPs marked as fulfilled
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE ptp SET status = 'FULFILLED'
                WHERE phone_number = %s AND status = 'PENDING'
            """, (phone_number,))
            count = cur.rowcount
            conn.commit()
            if count > 0:
                logger.info(
                    "Marked PTPs as FULFILLED",
                    extra={
                        "event": "ptp_fulfilled",
                        "count": count,
                        "phone_number": phone_number,
                    },
                )
            return count


def mark_expired_ptps_missed() -> int:
    """
    Mark all pending PTPs with past promise_date as MISSED.
    Called once a day by the worker (hour configurable via PTP_CHECK_HOUR).
    
    Returns:
        Number of PTPs marked as missed
    """
    today = today_local().isoformat()  # local (configured) date, not UTC
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE ptp SET status = 'MISSED'
                WHERE status = 'PENDING' 
                  AND promise_date < %s::date
            """, (today,))
            count = cur.rowcount
            conn.commit()
            return count


def update_borrower_paid(phone_number: str) -> bool:
    """
    Update borrower record when payment is confirmed.
    Sets status to PAID and billing_amount to 0.
    
    Args:
        phone_number: User's phone number
    
    Returns:
        True if updated, False if borrower not found
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE borrowers 
                SET status = 'PAID', 
                    billing_amount = 0,
                    days_late = 0
                WHERE phone_number = %s
                RETURNING phone_number
            """, (phone_number,))
            result = cur.fetchone()
            conn.commit()
            if result:
                logger.info(
                    "Updated borrower to PAID",
                    extra={"event": "borrower_paid", "phone_number": phone_number},
                )
            return result is not None


def update_all_days_late() -> tuple[int, int]:
    """
    Recalculate days_late and status for all unpaid borrowers.
    Uses the configured local timezone (TIMEZONE) for accurate date calculation.
    
    Logic:
    - days_late = today_local - due_date (negative = upcoming, positive = overdue)
    - If days_late <= 0: status = 'UPCOMING'
    - If days_late > 0: status = 'OVERDUE'
    - Skip borrowers with status = 'PAID'
    
    Returns:
        (total_updated, status_changed_count)
    """
    today = today_local().isoformat()  # YYYY-MM-DD string
    
    with get_connection() as conn:
        with conn.cursor() as cur:
            # First, count how many will have status changes
            cur.execute("""
                SELECT COUNT(*) as cnt FROM borrowers
                WHERE status != 'PAID' AND billing_amount > 0
                  AND (
                    (status = 'UPCOMING' AND %s::date - due_date::date > 0)
                    OR (status = 'OVERDUE' AND %s::date - due_date::date <= 0)
                  )
            """, (today, today))
            status_changed = cur.fetchone()["cnt"]
            
            # Now update all unpaid borrowers
            cur.execute("""
                UPDATE borrowers
                SET 
                    days_late = %s::date - due_date::date,
                    status = CASE 
                        WHEN %s::date - due_date::date <= 0 THEN 'UPCOMING'
                        ELSE 'OVERDUE'
                    END
                WHERE status != 'PAID' AND billing_amount > 0
            """, (today, today))
            total_updated = cur.rowcount
            conn.commit()
            
            return total_updated, status_changed


# ============================================================================
# MORNING FOLLOW-UP (9 AM)
# ============================================================================

def get_followup_eligible_borrowers() -> list[dict]:
    """
    Get borrowers eligible for 9 AM morning follow-up.

    Eligibility criteria:
    - days_late > 0 (already late)
    - status != 'PAID' (not paid)
    - session status != 'human_handoff'
    - had interaction within last 24 hours (for Meta's free message window)
    - no PENDING PTP (any pending PTP excludes borrower)
    - no pending scheduled_tasks (payment verification in progress)
    - NOT in control group (A/B test - excludes labels like NB1, RB1, LB1)

    Returns:
        List of dicts with:
        - phone_number, customer_name, days_late, billing_amount
    """
    today = today_local().isoformat()  # YYYY-MM-DD in configured timezone

    with get_connection() as conn:
        with conn.cursor() as cur:
            query = """
                SELECT DISTINCT
                    b.phone_number,
                    b.customer_name,
                    b.days_late,
                    b.billing_amount
                FROM borrowers b
                JOIN chat_sessions cs ON b.phone_number = cs.phone_number
                WHERE b.days_late > 0
                  AND b.status != 'PAID'
                  AND cs.status != 'human_handoff'
                  AND cs.last_message_at >= NOW() - INTERVAL '24 hours'
                  AND (cs.last_followup_at IS NULL OR cs.last_followup_at < %s::date)
                  AND NOT EXISTS (
                      SELECT 1 FROM ptp p
                      WHERE p.phone_number = b.phone_number
                        AND p.status = 'PENDING'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM scheduled_tasks st
                      WHERE st.phone_number = b.phone_number
                        AND st.status = 'pending'
                  )
            """

            # Add control group filter if configured
            params: list = [today]
            if CONTROL_GROUP_LABELS:
                query += "  AND (b.label IS NULL OR b.label NOT IN %s)\n"
                params.append(tuple(CONTROL_GROUP_LABELS))

            query += "ORDER BY b.phone_number"

            cur.execute(query, params)
            return cur.fetchall()


def save_followup_message(phone_number: str, message: str) -> None:
    """
    Save follow-up message to chat_history.
    Uses 'bot-followup' sender and [FOLLOWUP] prefix for identification.

    Args:
        phone_number: User's phone number
        message: The follow-up message content
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO chat_history (phone_number, sender, message_content, is_processed, created_at)
                VALUES (%s, 'bot-followup', %s, TRUE, NOW())
            """, (phone_number, f"[FOLLOWUP] {message}"))
            conn.commit()


def update_followup_sent(phone_number: str) -> None:
    """
    Mark that follow-up was sent to this borrower today.
    Prevents duplicate follow-ups if worker restarts after 9 AM.

    Args:
        phone_number: User's phone number
    """
    today = today_local().isoformat()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE chat_sessions
                SET last_followup_at = %s::date
                WHERE phone_number = %s
            """, (today, phone_number))
            conn.commit()


# ============================================================================
# PTP REMINDERS
# ============================================================================

def ensure_ptp_reminders_table() -> None:
    """Create ptp_reminders table if it doesn't exist (for deduplication)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ptp_reminders (
                    id SERIAL PRIMARY KEY,
                    ptp_id INTEGER NOT NULL REFERENCES ptp(id),
                    phone_number TEXT NOT NULL,
                    template_name TEXT NOT NULL,
                    campaign_id TEXT,
                    sent_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    reminder_date DATE NOT NULL,
                    UNIQUE(ptp_id, reminder_date, template_name)
                );
                CREATE INDEX IF NOT EXISTS idx_ptp_reminders_date ON ptp_reminders(reminder_date);
                CREATE INDEX IF NOT EXISTS idx_ptp_reminders_ptp_id ON ptp_reminders(ptp_id);
            """)
            conn.commit()
    logger.info("Ensured ptp_reminders table exists", extra={
        "event": "ptp_reminders_table_ensured",
    })


def get_ptp_due_today(template_name: str) -> list[dict]:
    """
    Query PENDING PTPs due today that haven't been reminded yet.
    Excludes users in human_handoff or with pending scheduled_tasks.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.id as ptp_id, p.phone_number, p.promise_amount, p.promise_date,
                       b.customer_name, b.customer_number
                FROM ptp p
                JOIN borrowers b ON p.phone_number = b.phone_number
                WHERE p.status = 'PENDING'
                  AND p.promise_date = CURRENT_DATE
                  AND NOT EXISTS (
                    SELECT 1 FROM ptp_reminders r
                    WHERE r.ptp_id = p.id
                      AND r.reminder_date = CURRENT_DATE
                      AND r.template_name = %s
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM chat_sessions cs
                    WHERE cs.phone_number = p.phone_number
                      AND cs.status = 'human_handoff'
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM scheduled_tasks st
                    WHERE st.phone_number = p.phone_number
                      AND st.status = 'pending'
                  )
            """, (template_name,))
            return cur.fetchall()


def get_ptp_due_tomorrow(template_name: str) -> list[dict]:
    """
    Query PENDING PTPs due tomorrow (D-1 reminder) that haven't been reminded yet.
    Excludes users in human_handoff or with pending scheduled_tasks.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.id as ptp_id, p.phone_number, p.promise_amount, p.promise_date,
                       b.customer_name, b.customer_number
                FROM ptp p
                JOIN borrowers b ON p.phone_number = b.phone_number
                WHERE p.status = 'PENDING'
                  AND p.promise_date = CURRENT_DATE + INTERVAL '1 day'
                  AND NOT EXISTS (
                    SELECT 1 FROM ptp_reminders r
                    WHERE r.ptp_id = p.id
                      AND r.reminder_date = CURRENT_DATE
                      AND r.template_name = %s
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM chat_sessions cs
                    WHERE cs.phone_number = p.phone_number
                      AND cs.status = 'human_handoff'
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM scheduled_tasks st
                    WHERE st.phone_number = p.phone_number
                      AND st.status = 'pending'
                  )
            """, (template_name,))
            return cur.fetchall()


def get_missed_ptp_yesterday(template_name: str) -> list[dict]:
    """
    Query MISSED PTPs from yesterday that haven't been reminded yet.
    Excludes users in human_handoff.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.id as ptp_id, p.phone_number, p.promise_amount, p.promise_date,
                       b.customer_name, b.customer_number, b.billing_amount
                FROM ptp p
                JOIN borrowers b ON p.phone_number = b.phone_number
                WHERE p.status = 'MISSED'
                  AND p.promise_date = CURRENT_DATE - INTERVAL '1 day'
                  AND NOT EXISTS (
                    SELECT 1 FROM ptp_reminders r
                    WHERE r.ptp_id = p.id
                      AND r.template_name = %s
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM chat_sessions cs
                    WHERE cs.phone_number = p.phone_number
                      AND cs.status = 'human_handoff'
                  )
            """, (template_name,))
            return cur.fetchall()


def log_ptp_reminders(rows: list[dict], template_name: str, campaign_id: str | None, reminder_date) -> None:
    """Log sent PTP reminders to ptp_reminders table for deduplication."""
    if not rows:
        return
    with get_connection() as conn:
        with conn.cursor() as cur:
            for row in rows:
                cur.execute("""
                    INSERT INTO ptp_reminders (ptp_id, phone_number, template_name, campaign_id, reminder_date)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (ptp_id, reminder_date, template_name) DO NOTHING
                """, (row["ptp_id"], row["phone_number"], template_name, campaign_id, reminder_date))
            conn.commit()
    logger.info("Logged PTP reminders", extra={
        "event": "ptp_reminders_logged",
        "template": template_name,
        "count": len(rows),
    })


def log_ptp_to_chat_history(entries: list[tuple[str, str]]) -> None:
    """
    Batch-log PTP reminder messages to chat_history for conversation continuity.

    Args:
        entries: List of (phone_number, message) tuples
    """
    if not entries:
        return
    with get_connection() as conn:
        with conn.cursor() as cur:
            for phone_number, message in entries:
                cur.execute("""
                    INSERT INTO chat_history (phone_number, sender, message_content, is_processed, created_at)
                    VALUES (%s, 'bot-ptp', %s, TRUE, NOW())
                """, (phone_number, message))
            conn.commit()
    logger.info("Logged PTP messages to chat_history", extra={
        "event": "ptp_chat_history_logged",
        "count": len(entries),
    })
