"""
Manual payment verification script for pending scheduled tasks.
Processes all pending payment_check tasks regardless of scheduled_at time.

Usage:
    python3 -m scripts.manual_payment_check [--dry-run]
"""

import os
import sys
import time
import logging
import argparse

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from src.payment_checker import check_payment_status
from src.messaging import get_messaging_adapter
from src.followup_messages import get_payment_confirmed_message
from src.database_pg import (
    get_connection,
    mark_ptps_fulfilled,
    update_borrower_paid,
    mark_task_completed,
    mark_task_failed,
    save_bot_response,
    set_human_handoff,
)


def send_whatsapp_message(phone_number: str, message: str) -> dict:
    """Send via the configured messaging adapter (same shape as worker.py)."""
    return dict(get_messaging_adapter().send_text(phone_number, message))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

# Rate limiting
API_DELAY = 1  # seconds between API calls


def get_all_pending_tasks():
    """Get ALL pending payment_check tasks (ignore scheduled_at)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, phone_number, customer_number, task_type, 
                       scheduled_at, status, created_at
                FROM scheduled_tasks
                WHERE status = 'pending' AND task_type = 'payment_check'
                ORDER BY created_at
            """)
            return cur.fetchall()


def process_task(task, dry_run=False):
    """Process a single payment verification task."""
    task_id = task["id"]
    phone_number = task["phone_number"]
    customer_number = task["customer_number"]
    
    print(f"  Checking {customer_number} ({phone_number})...")
    
    # Check payment status via API
    result = check_payment_status(customer_number)
    status = result["status"]
    
    if status == "paid":
        print(f"    -> PAID! Sending confirmation...")
        
        if dry_run:
            print(f"    [DRY-RUN] Would send message and update DB")
            return "paid", True
        
        # Mark PTPs as fulfilled
        fulfilled_count = mark_ptps_fulfilled(phone_number)
        if fulfilled_count > 0:
            print(f"    Marked {fulfilled_count} PTP(s) as FULFILLED")
        
        # Update borrower status
        if update_borrower_paid(phone_number):
            print(f"    Updated borrower to PAID")
        
        # Send confirmation message
        message = get_payment_confirmed_message()
        send_result = send_whatsapp_message(phone_number, message)
        
        if send_result["success"]:
            save_bot_response(phone_number, message)
            mark_task_completed(task_id, "Payment confirmed (manual check)")
            print(f"    Message sent! (Mimin ID: {send_result['message_id']})")
            return "paid", True
        else:
            save_bot_response(phone_number, message)
            mark_task_failed(task_id, f"WhatsApp send failed: {send_result['error']}")
            print(f"    WARNING: Message saved but send failed: {send_result['error']}")
            return "paid", False
    
    else:  # needs_human
        print(f"    -> NOT CONFIRMED (needs human verification)")
        
        if dry_run:
            print(f"    [DRY-RUN] Would escalate to human CS")
            return "needs_human", True
        
        # Silent escalate to CS
        set_human_handoff(phone_number, "Payment verification inconclusive (manual check)")
        mark_task_completed(task_id, f"Silent handoff - manual check: {result['raw_response'][:100]}")
        print(f"    Escalated to human CS (silent handoff)")
        return "needs_human", True


def main():
    parser = argparse.ArgumentParser(description="Manual payment verification")
    parser.add_argument("--dry-run", action="store_true", help="Check status without sending messages")
    args = parser.parse_args()
    
    print("=" * 60)
    print("MANUAL PAYMENT VERIFICATION")
    print("=" * 60)
    
    if args.dry_run:
        print("*** DRY-RUN MODE - No messages will be sent ***\n")
    
    # Get all pending tasks
    tasks = get_all_pending_tasks()
    print(f"Found {len(tasks)} pending task(s)\n")
    
    if not tasks:
        print("No pending tasks to process.")
        return
    
    # Process each task
    paid_count = 0
    needs_human_count = 0
    failed_count = 0
    
    for i, task in enumerate(tasks, 1):
        print(f"[{i}/{len(tasks)}] Task {task['id']}:")
        try:
            status, success = process_task(task, args.dry_run)
            if status == "paid":
                paid_count += 1
            else:
                needs_human_count += 1
            if not success:
                failed_count += 1
        except Exception as e:
            print(f"    ERROR: {e}")
            failed_count += 1
        
        # Rate limiting
        if i < len(tasks):
            time.sleep(API_DELAY)
    
    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total processed: {len(tasks)}")
    print(f"  Paid (message sent): {paid_count}")
    print(f"  Needs human review:  {needs_human_count}")
    print(f"  Failed:              {failed_count}")


if __name__ == "__main__":
    main()
