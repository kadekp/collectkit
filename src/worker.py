"""
collectkit worker - background polling process.

Polls the database for chat sessions needing a reply, aggregates fragmented
user messages, invokes the LangGraph agent, and sends replies.

Usage:
    python -m src.worker

Note: New Relic is initialized via newrelic-admin in railway.toml.
"""

import newrelic.agent
import os
import sys
import time
import signal
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, AIMessage
from langsmith import Client as LangSmithClient

# Load environment before other imports
load_dotenv()

# Setup structured logging BEFORE any logger usage
from .logging_config import setup_logging, get_logger
setup_logging()
logger = get_logger(__name__)

# Import metrics and health modules
from .metrics import metrics
from .health import start_health_server, set_background_thread_status

# Thread-safe shutdown event (replaces shutdown_flag)
shutdown_event = threading.Event()


def handle_shutdown(signum, frame):
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    signal_name = signal.Signals(signum).name
    logger.info("Shutdown signal received", extra={
        "event": "shutdown_signal",
        "signal": signal_name,
    })
    shutdown_event.set()


from .startup import validate_env  # re-exported for callers


def setup_langsmith():
    """Setup and validate LangSmith tracing if configured."""
    tracing_enabled = os.getenv("LANGCHAIN_TRACING_V2", "").lower() == "true"
    api_key = os.getenv("LANGCHAIN_API_KEY")
    project = os.getenv("LANGCHAIN_PROJECT", "my-bot")
    
    if tracing_enabled and api_key:
        try:
            client = LangSmithClient()
            logger.info("LangSmith tracing enabled", extra={
                "event": "langsmith_enabled",
                "project": project,
            })
            return True
        except Exception as e:
            logger.warning("LangSmith tracing connection failed", extra={
                "event": "langsmith_failed",
                "error": str(e),
            })
            return False
    else:
        logger.info("LangSmith tracing disabled", extra={
            "event": "langsmith_disabled",
        })
        return False

from psycopg_pool import ConnectionPool
from langgraph.checkpoint.postgres import PostgresSaver
from .database_pg import (
    get_sessions_needing_reply,
    lock_session,
    unlock_session,
    get_unprocessed_messages_with_images,
    mark_messages_processed,
    save_bot_response,
    get_database_url,
    clear_chat_history,
    clear_chat_session,
    clear_langgraph_checkpoints,
    clear_ptp_records,
    clear_scheduled_tasks,
    set_human_handoff,
    fetch_borrower_data,
    get_due_scheduled_tasks,
    mark_task_completed,
    mark_task_failed,
    mark_ptps_fulfilled,
    mark_expired_ptps_missed,
    update_borrower_paid,
    update_all_days_late,
    get_borrowers_for_payment_check,
    get_pending_scheduled_task_for_borrower,
    get_followup_eligible_borrowers,
    save_followup_message,
    update_followup_sent,
    set_session_needs_retry,
    reset_error_count,
    cleanup_paid_borrower_memory,
    is_excluded_number,
    ensure_ptp_reminders_table,
)
from .timezone_utils import now_local
from .image_analyzer import analyze_image
from .payment_checker import check_payment_status
from .agent import build_graph
from .messaging import get_messaging_adapter


def send_whatsapp_message(phone_number: str, message: str) -> dict:
    """Send a message via the configured messaging adapter.

    Kept as a thin wrapper so call sites read naturally. The return shape
    matches the legacy Mimin contract for backward compatibility.
    """
    return dict(get_messaging_adapter().send_text(phone_number, message))
from .guardrails import validate_input, validate_output
from .followup_messages import get_followup_message, get_payment_confirmed_message
from .ptp_reminder import process_ptp_reminders

# Configuration
from .config import get_config

_cfg = get_config()

POLL_INTERVAL = 2  # seconds between polls
DEBOUNCE_SECONDS = 4  # seconds to wait after last message before replying
RESET_COMMAND = "../new"  # Command to reset conversation
SCHEDULER_POLL_INTERVAL = 60  # seconds between scheduled task checks

# Scheduled-job hours, in the configured local timezone (see TIMEZONE env var).
# Override via PTP_CHECK_HOUR, BULK_SYNC_HOURS, FOLLOWUP_HOUR, PTP_REMINDER_HOUR.
PTP_CHECK_HOUR_LOCAL = _cfg.ptp_check_hour          # default 23 (11 PM)
BULK_SYNC_HOURS_LOCAL = _cfg.bulk_sync_hours        # default [7, 19]
FOLLOWUP_HOUR_LOCAL = _cfg.followup_hour            # default 9
PTP_REMINDER_HOUR_LOCAL = _cfg.ptp_reminder_hour    # default 9

BULK_SYNC_CONCURRENT_WORKERS = 3  # parallel API calls for payment checking
BULK_SYNC_API_DELAY = 0.1  # seconds between API calls (reduced due to parallelism)
FOLLOWUP_API_DELAY = 1  # seconds between follow-up sends (rate limiting)

# A/B Test - control group labels (no chatbot responses)
# Configure via env: CONTROL_GROUP_LABELS=NB1,RB1,LB1
_control_labels_raw = os.getenv("CONTROL_GROUP_LABELS", "")
CONTROL_GROUP_LABELS = frozenset(
    label.strip() for label in _control_labels_raw.split(",") if label.strip()
)


def handle_guardrail_failure(phone_number: str, guardrail_name: str) -> None:
    """Handle guardrail failure by triggering silent human handoff."""
    logger.warning("Guardrail triggered", extra={
        "event": "guardrail_triggered",
        "phone_number": phone_number,
        "guardrail": guardrail_name,
    })
    metrics.increment("guardrail_triggers", tags={"type": guardrail_name})
    set_human_handoff(phone_number, f"Guardrail: {guardrail_name}")
    mark_messages_processed(phone_number)


@newrelic.agent.background_task()
def process_ptp_expiry() -> None:
    """
    Check for expired PTPs and mark them as MISSED.
    Called once daily (hour from PTP_CHECK_HOUR, local time). No outbound message is sent.
    """
    try:
        count = mark_expired_ptps_missed()
        if count > 0:
            logger.info("PTP expiry processed", extra={
                "event": "ptp_expiry",
                "count": count,
            })
            metrics.increment("ptp_expired", value=count)
    except Exception as e:
        logger.error("PTP expiry processing failed", extra={
            "event": "ptp_expiry_error",
            "error": str(e),
        }, exc_info=True)
        metrics.increment("errors", tags={"type": "ptp_expiry"})


@newrelic.agent.background_task()
def process_paid_borrower_cleanup() -> None:
    """
    Clear stale bot memory for PAID (paid) borrowers.
    Called daily alongside PTP expiry (hour from PTP_CHECK_HOUR, local time).

    Clears LangGraph checkpoints, chat_sessions, and scheduled_tasks
    so the bot starts fresh if the borrower returns with a new billing cycle.
    Preserves chat_history (audit) and ptp (analytics).
    """
    try:
        result = cleanup_paid_borrower_memory()
        if any(result.values()):
            logger.info("Paid borrower memory cleanup completed", extra={
                "event": "paid_cleanup",
                **result,
            })
            metrics.increment("paid_cleanup", value=result["checkpoints"])
    except Exception as e:
        logger.error("Paid borrower cleanup failed", extra={
            "event": "paid_cleanup_error",
            "error": str(e),
        }, exc_info=True)
        metrics.increment("errors", tags={"type": "paid_cleanup"})


@newrelic.agent.background_task()
def process_scheduled_tasks() -> None:
    """
    Process all due scheduled tasks (payment verifications).
    
    For each due task:
    1. Check payment status via payment link
    2. Send appropriate follow-up message
    3. Mark task as completed/failed
    """
    try:
        due_tasks = get_due_scheduled_tasks()
        
        if not due_tasks:
            return
            
        logger.info("Processing scheduled tasks", extra={
            "event": "scheduled_tasks_start",
            "count": len(due_tasks),
        })
        
        for task in due_tasks:
            # Check for shutdown before processing each task
            if shutdown_event.is_set():
                logger.info("Scheduled tasks interrupted by shutdown", extra={
                    "event": "scheduled_tasks_shutdown",
                })
                break
            
            try:
                logger.info("Processing scheduled task", extra={
                    "event": "scheduled_task_start",
                    "task_id": task.id,
                    "task_type": task.task_type,
                    "phone_number": task.phone_number,
                })
                
                if task.task_type == "payment_check":
                    # Check payment status
                    result = check_payment_status(task.customer_number)
                    status = result["status"]

                    # Compose message based on result
                    if status == "paid":
                        # Mark any pending PTPs as fulfilled
                        fulfilled_count = mark_ptps_fulfilled(task.phone_number)
                        if fulfilled_count > 0:
                            logger.info("PTPs marked as fulfilled", extra={
                                "event": "ptp_fulfilled",
                                "phone_number": task.phone_number,
                                "count": fulfilled_count,
                            })
                        
                        # Update borrower status to PAID
                        if update_borrower_paid(task.phone_number):
                            logger.info("Borrower marked as paid", extra={
                                "event": "borrower_paid",
                                "phone_number": task.phone_number,
                            })
                        
                        message = get_payment_confirmed_message()
                        task_result = "Payment confirmed"
                    elif status == "needs_human":
                        # Silent escalate to human CS for manual verification
                        set_human_handoff(task.phone_number, "Payment verification inconclusive")
                        mark_task_completed(task.id, f"Silent handoff - verification inconclusive: {result['raw_response'][:100]}")
                        logger.info("Scheduled task escalated to human", extra={
                            "event": "scheduled_task_handoff",
                            "task_id": task.id,
                            "phone_number": task.phone_number,
                        })
                        metrics.increment("scheduled_tasks", tags={"status": "handoff"})
                        continue  # Skip message sending, go to next task
                    else:
                        # Fallback for any unexpected status
                        logger.error("Unexpected payment check status", extra={
                            "event": "scheduled_task_unexpected_status",
                            "task_id": task.id,
                            "status": status,
                        })
                        mark_task_failed(task.id, f"Unexpected status: {status}")
                        metrics.increment("scheduled_tasks", tags={"status": "failed"})
                        continue
                    
                    # Send follow-up message via WhatsApp
                    send_result = send_whatsapp_message(task.phone_number, message)
                    
                    if send_result["success"]:
                        # Save bot response to chat history
                        save_bot_response(task.phone_number, message, sender='bot-task')
                        mark_task_completed(task.id, task_result)
                        logger.info("Scheduled task completed", extra={
                            "event": "scheduled_task_completed",
                            "task_id": task.id,
                            "phone_number": task.phone_number,
                            "status": "paid",
                            "mimin_id": send_result["message_id"],
                        })
                        metrics.increment("scheduled_tasks", tags={"status": "completed"})
                        metrics.increment("payment_checks", tags={"status": "paid"})
                    else:
                        # Message saved but not sent
                        save_bot_response(task.phone_number, message, sender='bot-task')
                        mark_task_failed(task.id, f"WhatsApp send failed: {send_result['error']}")
                        logger.error("Scheduled task message send failed", extra={
                            "event": "scheduled_task_send_failed",
                            "task_id": task.id,
                            "phone_number": task.phone_number,
                            "error": send_result["error"],
                        })
                        metrics.increment("scheduled_tasks", tags={"status": "send_failed"})
                else:
                    # Unknown task type
                    mark_task_failed(task.id, f"Unknown task type: {task.task_type}")
                    logger.warning("Unknown task type", extra={
                        "event": "scheduled_task_unknown_type",
                        "task_id": task.id,
                        "task_type": task.task_type,
                    })
                    
            except Exception as e:
                logger.error("Scheduled task processing error", extra={
                    "event": "scheduled_task_error",
                    "task_id": task.id,
                    "error": str(e),
                }, exc_info=True)
                mark_task_failed(task.id, str(e)[:200])
                metrics.increment("errors", tags={"type": "scheduled_task"})
                
    except Exception as e:
        logger.error("Failed to fetch scheduled tasks", extra={
            "event": "scheduled_tasks_fetch_error",
            "error": str(e),
        }, exc_info=True)
        metrics.increment("errors", tags={"type": "scheduled_tasks_fetch"})


def _check_single_borrower_payment(borrower: dict) -> dict:
    """
    Check payment status for a single borrower (thread-safe helper).
    
    Args:
        borrower: Dict with phone_number, customer_number, customer_name
        
    Returns:
        Dict with original borrower data + payment_status
    """
    try:
        result = check_payment_status(borrower["customer_number"])
        return {
            **borrower,
            "payment_status": result["status"],
            "error": None,
        }
    except Exception as e:
        return {
            **borrower,
            "payment_status": "error",
            "error": str(e),
        }


@newrelic.agent.background_task()
def process_bulk_borrower_sync(current_hour: int) -> None:
    """
    Bulk sync borrower data. Runs at the hours listed in BULK_SYNC_HOURS (local time, default 7 and 19).
    
    Step 1: Recalculate days_late and status for all unpaid borrowers
    Step 2: Check payment status via API (CONCURRENT) with priority-based filtering:
        - 7 AM: Check ALL borrowers with outstanding bills
        - 7 PM: Check only HIGH priority (pending PTP or chatted in last 7 days)
    Step 3: Process paid borrowers sequentially (DB updates + WhatsApp)
    
    Payment check logic:
    1. Get borrowers based on priority (high priority only at 7 PM, all at 7 AM)
    2. For each borrower, check payment status via API (5 concurrent workers)
    3. If PAID:
       a. Update borrowers table (status=PAID, billing_amount=0)
       b. Update ptp table (mark PENDING as FULFILLED)
       c. If has pending scheduled_task: complete task + send message
       d. If NO pending scheduled_task: silent update (no message)
    4. If NOT PAID: skip (no action needed)
    
    Args:
        current_hour: Current hour in the configured local timezone (e.g. 7 or 19)
    """
    try:
        sync_start_time = time.time()
        
        # === STEP 1: Update days_late (fast, no API calls) ===
        updated_count, status_changed = update_all_days_late()
        logger.info("Bulk sync days_late updated", extra={
            "event": "bulk_sync_days_late",
            "updated": updated_count,
            "status_changed": status_changed,
        })
        
        # === STEP 2: Check payment status (CONCURRENT API calls) ===
        # Priority-based: 7 AM checks all, 7 PM checks only high priority
        if current_hour == 7:
            priority = "all"
        else:
            priority = "high"
        
        borrowers = get_borrowers_for_payment_check(priority=priority)
        
        if not borrowers:
            logger.info("Bulk sync completed - no outstanding bills", extra={
                "event": "bulk_sync_no_borrowers",
            })
            return
        
        total_borrowers = len(borrowers)
        logger.info("Bulk sync payment check started", extra={
            "event": "bulk_sync_payment_check_start",
            "count": total_borrowers,
            "priority": priority,
            "workers": BULK_SYNC_CONCURRENT_WORKERS,
        })
        
        # Counters for summary
        checked = 0
        errors = 0
        paid_borrowers = []  # Collect paid borrowers for sequential processing
        
        # Phase 1: Concurrent API calls to check payment status
        start_time = time.time()
        
        with ThreadPoolExecutor(max_workers=BULK_SYNC_CONCURRENT_WORKERS) as executor:
            # Submit all tasks
            future_to_borrower = {
                executor.submit(_check_single_borrower_payment, b): b 
                for b in borrowers
            }
            
            # Process results as they complete
            for future in as_completed(future_to_borrower):
                # Check for shutdown
                if shutdown_event.is_set():
                    logger.info("Bulk sync shutdown requested", extra={
                        "event": "bulk_sync_shutdown",
                    })
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                
                try:
                    result = future.result()
                    checked += 1
                    
                    if result["error"]:
                        errors += 1
                        logger.error("Bulk sync payment check error", extra={
                            "event": "bulk_sync_check_error",
                            "phone_number": result["phone_number"],
                            "error": result["error"],
                        })
                    elif result["payment_status"] == "paid":
                        paid_borrowers.append(result)
                    
                    # Progress logging every 500 borrowers
                    if checked % 500 == 0:
                        elapsed = time.time() - start_time
                        rate = checked / elapsed if elapsed > 0 else 0
                        logger.info("Bulk sync progress", extra={
                            "event": "bulk_sync_progress",
                            "checked": checked,
                            "total": total_borrowers,
                            "percent": checked * 100 // total_borrowers,
                            "rate": round(rate, 1),
                        })
                        
                except Exception as e:
                    errors += 1
                    original = future_to_borrower[future]
                    logger.error("Bulk sync future error", extra={
                        "event": "bulk_sync_future_error",
                        "phone_number": original.get("phone_number", "unknown"),
                        "error": str(e),
                    })
        
        api_elapsed = time.time() - start_time
        logger.info("Bulk sync API phase completed", extra={
            "event": "bulk_sync_api_completed",
            "duration_ms": round(api_elapsed * 1000),
            "checked": checked,
            "paid": len(paid_borrowers),
            "errors": errors,
        })
        
        # Phase 2: Sequential processing of paid borrowers (DB updates + WhatsApp)
        if shutdown_event.is_set():
            logger.info("Bulk sync DB updates skipped due to shutdown", extra={
                "event": "bulk_sync_db_skipped",
            })
            return
            
        messages_sent = 0
        
        for borrower in paid_borrowers:
            if shutdown_event.is_set():
                logger.info("Bulk sync DB updates interrupted", extra={
                    "event": "bulk_sync_db_interrupted",
                })
                break
                
            phone_number = borrower["phone_number"]
            customer_name = borrower["customer_name"]
            
            try:
                logger.info("Bulk sync payment confirmed", extra={
                    "event": "bulk_sync_payment_confirmed",
                    "phone_number": phone_number,
                    "customer_name": customer_name,
                })
                
                # Update borrower status to PAID
                if update_borrower_paid(phone_number):
                    logger.debug("Borrower updated to PAID", extra={
                        "phone_number": phone_number,
                    })
                
                # Mark any pending PTPs as fulfilled
                fulfilled_count = mark_ptps_fulfilled(phone_number)
                if fulfilled_count > 0:
                    logger.debug("PTPs fulfilled in bulk sync", extra={
                        "phone_number": phone_number,
                        "count": fulfilled_count,
                    })
                
                # Check if there's a pending scheduled task
                pending_task = get_pending_scheduled_task_for_borrower(phone_number)
                
                if pending_task:
                    # Has pending task: complete it and send message
                    message = get_payment_confirmed_message()
                    
                    send_result = send_whatsapp_message(phone_number, message)
                    
                    if send_result["success"]:
                        save_bot_response(phone_number, message, sender='bot-task')
                        mark_task_completed(pending_task.id, "Payment confirmed (bulk sync)")
                        messages_sent += 1
                        logger.info("Bulk sync task completed with message", extra={
                            "event": "bulk_sync_task_completed",
                            "task_id": pending_task.id,
                            "phone_number": phone_number,
                            "mimin_id": send_result["message_id"],
                        })
                    else:
                        save_bot_response(phone_number, message, sender='bot-task')
                        mark_task_failed(pending_task.id, f"WhatsApp send failed: {send_result['error']}")
                        logger.error("Bulk sync task message send failed", extra={
                            "event": "bulk_sync_task_send_failed",
                            "task_id": pending_task.id,
                            "phone_number": phone_number,
                            "error": send_result["error"],
                        })
                    
                    # Rate limit WhatsApp sends
                    time.sleep(BULK_SYNC_API_DELAY)
                else:
                    # No pending task: silent update (no message)
                    logger.debug("Bulk sync silent update", extra={
                        "phone_number": phone_number,
                    })
                    
            except Exception as e:
                logger.error("Bulk sync paid borrower processing error", extra={
                    "event": "bulk_sync_borrower_error",
                    "phone_number": phone_number,
                    "error": str(e),
                }, exc_info=True)
                metrics.increment("errors", tags={"type": "bulk_sync_borrower"})
        
        # Log final summary
        total_elapsed = time.time() - sync_start_time
        logger.info("Bulk sync completed", extra={
            "event": "bulk_sync_completed",
            "duration_ms": round(total_elapsed * 1000),
            "checked": checked,
            "paid": len(paid_borrowers),
            "sent": messages_sent,
            "errors": errors,
        })
        metrics.record_duration("bulk_sync_duration", total_elapsed * 1000)
        metrics.increment("bulk_sync_paid", value=len(paid_borrowers))
        
    except Exception as e:
        logger.error("Bulk sync failed", extra={
            "event": "bulk_sync_error",
            "error": str(e),
        }, exc_info=True)
        metrics.increment("errors", tags={"type": "bulk_sync"})


@newrelic.agent.background_task()
def process_morning_followup() -> None:
    """
    Send 9 AM follow-up messages to eligible borrowers.
    
    Runs once a day, Monday-Saturday only (hour from FOLLOWUP_HOUR, local time).
    Targets late borrowers (days_late > 0) who interacted within 24 hours
    but haven't paid yet.
    
    Exclusions:
    - Non-borrowers (not in borrowers table)
    - Human handoff sessions
    - Borrowers with pending PTP (unless PTP is due today)
    - Borrowers who already paid (PAID)
    - Borrowers not yet late (days_late <= 0)
    """
    try:
        followup_start_time = time.time()
        
        # Get eligible borrowers
        borrowers = get_followup_eligible_borrowers()
        
        if not borrowers:
            logger.info("No eligible borrowers for follow-up", extra={
                "event": "followup_no_borrowers",
            })
            return
        
        logger.info("Morning follow-up started", extra={
            "event": "followup_start",
            "count": len(borrowers),
        })
        
        # Counters for summary
        sent_count = 0
        failed_count = 0
        
        for borrower in borrowers:
            # Check for shutdown before processing each borrower
            if shutdown_event.is_set():
                logger.info("Follow-up interrupted by shutdown", extra={
                    "event": "followup_shutdown",
                })
                break
            
            phone_number = borrower["phone_number"]
            customer_name = borrower["customer_name"]
            days_late = borrower["days_late"]
            billing_amount = borrower["billing_amount"]
            
            try:
                # Generate appropriate message
                message = get_followup_message(
                    customer_name=customer_name,
                    days_late=days_late,
                    billing_amount=billing_amount,
                )
                
                # Send via WhatsApp
                send_result = send_whatsapp_message(phone_number, message)
                
                if send_result["success"]:
                    # Save to chat_history with [FOLLOWUP] prefix
                    save_followup_message(phone_number, message)
                    # Mark followup sent to prevent duplicates on worker restart
                    update_followup_sent(phone_number)
                    sent_count += 1
                    logger.info("Follow-up sent", extra={
                        "event": "followup_sent",
                        "phone_number": phone_number,
                        "days_late": days_late,
                        "mimin_id": send_result["message_id"],
                    })
                else:
                    failed_count += 1
                    logger.error("Follow-up send failed", extra={
                        "event": "followup_send_failed",
                        "phone_number": phone_number,
                        "error": send_result["error"],
                    })
                
            except Exception as e:
                failed_count += 1
                logger.error("Follow-up processing error", extra={
                    "event": "followup_error",
                    "phone_number": phone_number,
                    "error": str(e),
                }, exc_info=True)
            
            # Rate limiting: wait between sends
            time.sleep(FOLLOWUP_API_DELAY)
        
        # Log summary
        elapsed = time.time() - followup_start_time
        logger.info("Morning follow-up completed", extra={
            "event": "followup_completed",
            "duration_ms": round(elapsed * 1000),
            "sent": sent_count,
            "failed": failed_count,
        })
        metrics.increment("followup_sent", value=sent_count)
        metrics.increment("followup_failed", value=failed_count)
        
    except Exception as e:
        logger.error("Morning follow-up failed", extra={
            "event": "followup_error",
            "error": str(e),
        }, exc_info=True)
        metrics.increment("errors", tags={"type": "followup"})


# ============================================================================
# BACKGROUND TASK THREAD
# ============================================================================

BACKGROUND_POLL_INTERVAL = 10  # seconds between background task checks


@newrelic.agent.background_task()
def background_task_loop():
    """
    Background thread for slow/blocking scheduled tasks.
    
    Runs bulk sync, morning follow-up, scheduled tasks, and PTP expiry
    in a separate thread so they do NOT block the main chat polling loop.
    
    This ensures the chatbot remains responsive even when processing
    10,000+ borrowers during bulk operations.
    """
    logger.info("Background task thread started", extra={
        "event": "background_thread_started",
    })
    set_background_thread_status(True)
    
    # Track timing (local to this thread - thread-safe)
    last_scheduler_check = 0
    last_ptp_check_date = None
    last_bulk_sync_hour = None  # Tuple of (date, hour) to track last bulk sync
    last_followup_date = None
    last_ptp_reminder_date = None

    while not shutdown_event.is_set():
        try:
            current_time = time.time()
            current_local = now_local()
            current_date = current_local.date()
            
            # 1. Scheduled tasks (every SCHEDULER_POLL_INTERVAL seconds)
            if current_time - last_scheduler_check >= SCHEDULER_POLL_INTERVAL:
                process_scheduled_tasks()
                last_scheduler_check = current_time
            
            # 2. PTP expiry (daily at PTP_CHECK_HOUR, local time)
            if (current_local.hour == PTP_CHECK_HOUR_LOCAL and 
                last_ptp_check_date != current_date):
                process_ptp_expiry()
                process_paid_borrower_cleanup()
                last_ptp_check_date = current_date
            
            # 3. Bulk sync (at BULK_SYNC_HOURS, local time)
            if (current_local.hour in BULK_SYNC_HOURS_LOCAL and
                last_bulk_sync_hour != (current_date, current_local.hour)):
                process_bulk_borrower_sync(current_hour=current_local.hour)
                last_bulk_sync_hour = (current_date, current_local.hour)
            
            # 4. Morning follow-up (9 AM Mon-Sat, only during 9:00-9:59 window)
            is_weekday = current_local.weekday() < 6  # Mon-Sat
            if (is_weekday and
                current_local.hour == FOLLOWUP_HOUR_LOCAL and
                last_followup_date != current_date):
                process_morning_followup()
                last_followup_date = current_date

            # 5. PTP reminders (9 AM daily - due today, D-1, missed)
            if (current_local.hour == PTP_REMINDER_HOUR_LOCAL and
                last_ptp_reminder_date != current_date):
                process_ptp_reminders()
                last_ptp_reminder_date = current_date

        except Exception as e:
            logger.error("Background task loop error", extra={
                "event": "background_loop_error",
                "error": str(e),
            }, exc_info=True)
            metrics.increment("errors", tags={"type": "background_loop"})
        
        # Sleep between checks (responsive to shutdown via wait with timeout)
        shutdown_event.wait(timeout=BACKGROUND_POLL_INTERVAL)
    
    set_background_thread_status(False)
    logger.info("Background task thread stopped", extra={
        "event": "background_thread_stopped",
    })


@newrelic.agent.background_task()
def process_session(phone_number: str, graph) -> None:
    """
    Process a single user session.

    1. Lock session (status = 'processing')
    2. Fetch unprocessed messages
    3. Aggregate messages into single prompt
    4. Invoke LangGraph agent
    5. Save bot response to chat_history
    6. Mark user messages as processed
    7. Unlock session (status = 'idle')
    """
    session_start_time = time.time()
    
    # 1. Lock session (atomic - prevents double-processing)
    if not lock_session(phone_number):
        logger.debug("Session already being processed", extra={
            "phone_number": phone_number,
        })
        return

    try:
        # 2. Get unprocessed messages (including image data)
        messages = get_unprocessed_messages_with_images(phone_number)
        message_ids = [msg["id"] for msg in messages] if messages else []
        if not messages:
            logger.warning("No unprocessed messages found", extra={
                "phone_number": phone_number,
            })
            return

        # 3. Aggregate messages into single prompt (with image analysis)
        combined_parts = []
        image_count = 0

        for msg in messages:
            text = msg["message_content"]
            image_data = msg.get("image_data")

            # Check if this is an image message
            if image_data and text.startswith("[Image]"):
                image_count += 1
                caption = text.replace("[Image]", "").strip()
                logger.info("Analyzing image", extra={
                    "event": "image_analysis_start",
                    "phone_number": phone_number,
                    "image_count": image_count,
                })

                # Analyze image using vision LLM
                analysis = analyze_image(image_data, caption=caption)

                # Build enhanced text with analysis
                analysis_text = f"{text}\n\n[Image Analysis]\n- Summary: {analysis['summary']}"
                if analysis['ocr_text']:
                    analysis_text += f"\n- Detected text: {analysis['ocr_text']}"

                combined_parts.append(analysis_text)
            else:
                combined_parts.append(text)

        combined_text = "\n\n".join(combined_parts)

        if image_count > 0:
            logger.info("Images processed", extra={
                "event": "images_processed",
                "phone_number": phone_number,
                "count": image_count,
            })
            metrics.increment("images_analyzed", value=image_count)

        logger.info("Processing session", extra={
            "event": "session_processing",
            "phone_number": phone_number,
            "message_count": len(messages),
        })

        # Check bot exclusion list (before borrower lookup for efficiency)
        if is_excluded_number(phone_number):
            logger.info("Excluded number skipped", extra={
                "event": "excluded_number_skipped",
                "phone_number": phone_number,
            })
            metrics.increment("sessions_skipped", tags={"reason": "excluded"})
            mark_messages_processed(phone_number)
            return  # Exit early, no reply

        # Fetch borrower data (also serves as registration check)
        borrower = fetch_borrower_data(phone_number)
        if not borrower:
            logger.info("Non-borrower message skipped", extra={
                "event": "non_borrower_skipped",
                "phone_number": phone_number,
            })
            metrics.increment("sessions_skipped", tags={"reason": "non_borrower"})
            mark_messages_processed(phone_number)  # Mark as processed so we don't keep picking them up
            return  # Exit early, no reply

        # Check for control group (A/B test - no chatbot responses)
        if CONTROL_GROUP_LABELS and borrower.label in CONTROL_GROUP_LABELS:
            logger.info("Control group message skipped", extra={
                "event": "control_group_skipped",
                "phone_number": phone_number,
                "label": borrower.label,
            })
            metrics.increment("sessions_skipped", tags={"reason": "control_group"})
            mark_messages_processed(phone_number)
            return

        # Check for reset command
        if combined_text.strip().lower() == RESET_COMMAND:
            logger.info("Reset command received", extra={
                "event": "reset_command",
                "phone_number": phone_number,
            })
            
            # Clear chat history
            deleted_count = clear_chat_history(phone_number)
            
            # Clear chat session
            clear_chat_session(phone_number)
            
            # Clear LangGraph checkpoints
            clear_langgraph_checkpoints(phone_number)
            
            # Clear PTP records
            ptp_count = clear_ptp_records(phone_number)
            
            # Clear scheduled tasks
            task_count = clear_scheduled_tasks(phone_number)
            
            logger.info("Reset completed", extra={
                "event": "reset_completed",
                "phone_number": phone_number,
                "messages_cleared": deleted_count,
                "ptp_cleared": ptp_count,
                "tasks_cleared": task_count,
            })
            metrics.increment("resets")
            return  # Skip normal agent processing

        # === INPUT GUARDRAIL ===
        input_ok, guardrail_name = validate_input(combined_text)
        if not input_ok:
            handle_guardrail_failure(phone_number, guardrail_name)
            return

        # 4. Invoke LangGraph agent with run metadata
        config = {
            "configurable": {"thread_id": phone_number},
            "metadata": {
                "phone_number": phone_number,
                "session_id": phone_number,
                "message_count": len(messages),
                # Business metadata for LangSmith filtering/analysis
                "borrower_status": borrower.status,
                "days_late": borrower.days_late,
                "billing_amount": borrower.billing_amount,
                "has_image": any("[Image]" in m.get("message_content", "") for m in messages),
            },
            "run_name": f"chat-{phone_number[-4:]}",  # Last 4 digits for privacy
            "tags": ["production", borrower.status],
        }
        state = {
            "messages": [HumanMessage(content=combined_text)],
            "user_phone": phone_number,
            "borrower_details": None,
        }

        result = graph.invoke(state, config)

        # 5. Extract bot response (find last AIMessage with content)
        bot_response = None
        for msg in reversed(result["messages"]):
            if isinstance(msg, AIMessage) and msg.content:
                bot_response = msg.content
                break

        if bot_response:
            # === OUTPUT GUARDRAIL ===
            output_ok, guardrail_name = validate_output(bot_response)
            if not output_ok:
                handle_guardrail_failure(phone_number, guardrail_name)
                return

            # 6. Save bot response to chat_history
            save_bot_response(phone_number, bot_response)

            # 7. Send via Mimin.io WhatsApp API
            result = send_whatsapp_message(phone_number, bot_response)
            if result["success"]:
                elapsed = time.time() - session_start_time
                logger.info("Reply sent", extra={
                    "event": "reply_sent",
                    "phone_number": phone_number,
                    "mimin_id": result["message_id"],
                    "duration_ms": round(elapsed * 1000),
                    "borrower_status": borrower.status,
                    "days_late": borrower.days_late,
                })
                metrics.increment("messages_sent", tags={"status": "success"})
                metrics.record_duration("session_duration", elapsed * 1000)
            else:
                logger.error("Reply send failed", extra={
                    "event": "reply_failed",
                    "phone_number": phone_number,
                    "error": result["error"],
                })
                metrics.increment("messages_sent", tags={"status": "failed"})
        else:
            logger.warning("No bot response generated", extra={
                "event": "no_response",
                "phone_number": phone_number,
            })
            metrics.increment("sessions_no_response")

        # 8. Mark user messages as processed (only the ones we fetched,
        #    so messages arriving during processing aren't swept up)
        mark_messages_processed(phone_number, message_ids)
        reset_error_count(phone_number)
        metrics.increment("sessions_processed")

    except Exception as e:
        logger.error("Session processing error", extra={
            "event": "session_error",
            "phone_number": phone_number,
            "error": str(e),
        }, exc_info=True)
        metrics.increment("errors", tags={"type": "session_processing"})

        # Retry: set back to needs_reply so worker picks it up again
        MAX_RETRIES = 3
        try:
            retry_count = set_session_needs_retry(phone_number)
            if retry_count >= MAX_RETRIES:
                logger.error("Max retries reached, marking messages processed", extra={
                    "event": "max_retries_reached",
                    "phone_number": phone_number,
                    "retry_count": retry_count,
                })
                mark_messages_processed(phone_number, message_ids)
                reset_error_count(phone_number)
        except Exception:
            logger.error("Failed to set retry status", exc_info=True)

    finally:
        # 9. Unlock session (always, even on error)
        unlock_session(phone_number)


def main():
    """
    Main polling loop - handles ONLY chat sessions.
    
    Background tasks (bulk sync, follow-ups, scheduled tasks) run in a
    separate thread to prevent blocking the chatbot.
    """
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    # Validate environment variables
    validate_env()

    # Setup LangSmith monitoring
    langsmith_enabled = setup_langsmith()

    # Start health check server first so the container reports healthy even
    # if the worker is disabled (lets orchestrators keep it running).
    health_port = int(os.getenv("HEALTH_PORT", "8080"))
    health_server = start_health_server(port=health_port)

    # Check if worker is enabled (allows disabling via env var)
    worker_enabled = os.getenv("WORKER_ENABLED", "true").lower()
    if worker_enabled == "false":
        logger.info("Worker disabled via env var", extra={"event": "worker_disabled"})
        while not shutdown_event.is_set():
            time.sleep(60)  # Sleep in 60s intervals, check for shutdown signal
        logger.info("Worker stopped", extra={"event": "worker_stopped"})
        return

    logger.info("Worker starting", extra={
        "event": "worker_starting",
        "poll_interval": POLL_INTERVAL,
        "debounce_seconds": DEBOUNCE_SECONDS,
        "health_port": health_port,
    })

    # Setup PostgreSQL checkpointer for conversation persistence
    database_url = get_database_url()
    logger.info("Connecting to PostgreSQL", extra={"event": "db_connecting"})

    # Create connection pool and checkpointer
    pool = ConnectionPool(conninfo=database_url)
    checkpointer = PostgresSaver(pool)

    # Setup checkpointer tables (creates if not exist)
    # Use direct connection with autocommit for setup to avoid transaction issues
    import psycopg
    with psycopg.connect(database_url, autocommit=True) as setup_conn:
        setup_checkpointer = PostgresSaver(conn=setup_conn)
        setup_checkpointer.setup()

    # Ensure PTP reminders table exists
    ensure_ptp_reminders_table()

    # Build the LangGraph agent
    graph = build_graph(checkpointer=checkpointer)
    logger.info("LangGraph agent initialized", extra={"event": "agent_initialized"})

    # Start background task thread (daemon=True for auto-cleanup on exit)
    background_thread = threading.Thread(
        target=background_task_loop,
        name="BackgroundTasks",
        daemon=True
    )
    background_thread.start()

    logger.info("Worker ready", extra={
        "event": "worker_ready",
        "scheduler_interval": SCHEDULER_POLL_INTERVAL,
        "bulk_sync_hours": BULK_SYNC_HOURS_LOCAL,
        "followup_hour": FOLLOWUP_HOUR_LOCAL,
        "ptp_reminder_hour": PTP_REMINDER_HOUR_LOCAL,
        "ptp_check_hour": PTP_CHECK_HOUR_LOCAL,
    })

    # Main polling loop - ONLY handles chat sessions (never blocked by bulk tasks!)
    while not shutdown_event.is_set():
        try:
            # Poll for sessions needing reply (with debounce)
            sessions = get_sessions_needing_reply(DEBOUNCE_SECONDS)

            if sessions:
                logger.info("Sessions found needing reply", extra={
                    "event": "sessions_found",
                    "count": len(sessions),
                })

                for session in sessions:
                    if shutdown_event.is_set():
                        break
                    phone_number = session["phone_number"]
                    process_session(phone_number, graph)

        except Exception as e:
            logger.error("Main polling error", extra={
                "event": "polling_error",
                "error": str(e),
            }, exc_info=True)
            metrics.increment("errors", tags={"type": "polling"})

        # Wait before next poll
        if not shutdown_event.is_set():
            time.sleep(POLL_INTERVAL)

    # Graceful shutdown: wait for background thread to finish
    logger.info("Waiting for background thread to finish", extra={
        "event": "shutdown_waiting",
    })
    background_thread.join(timeout=30)
    if background_thread.is_alive():
        logger.warning("Background thread did not finish in time", extra={
            "event": "shutdown_timeout",
        })
    
    # Stop health server
    health_server.stop()
    
    logger.info("Worker stopped", extra={"event": "worker_stopped"})


if __name__ == "__main__":
    main()
