"""
health_check.py - Automated Watchdog
Checks the status of the Dual-Engine AI and reports to Telegram.
"""

import os
import subprocess
import sys
import datetime

# Ensure we can import notifier
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from notifier import send_alert

def check_service():
    try:
        # Check if service is active
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "deal-scout.service"],
            capture_output=True,
            text=True
        )
        is_active = result.stdout.strip() == "active"
        
        # Get memory and CPU usage
        status_result = subprocess.run(
            ["systemctl", "--user", "status", "deal-scout.service", "--no-pager"],
            capture_output=True,
            text=True
        )
        
        status_lines = status_result.stdout.split('\n')
        memory = "Unknown"
        tasks = "Unknown"
        for line in status_lines:
            if "Memory:" in line:
                memory = line.split("Memory:")[1].strip()
            elif "Tasks:" in line:
                tasks = line.split("Tasks:")[1].strip()

        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if is_active:
            msg = (
                f"🟢 <b>SYSTEM HEALTH: ONLINE</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"The Deal Scout Dual-Engine is running perfectly.\n\n"
                f"⏱️ <b>Time:</b> {now}\n"
                f"🧠 <b>Memory:</b> {memory}\n"
                f"🔄 <b>Tasks:</b> {tasks}\n\n"
                f"Both Telegram AI & Web AI Scraper are actively monitoring."
            )
        else:
            msg = (
                f"🔴 <b>CRITICAL ALERT: SYSTEM OFFLINE</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"The Deal Scout service has crashed or stopped!\n\n"
                f"⏱️ <b>Time:</b> {now}\n"
                f"Please SSH into the server and check the logs using:\n"
                f"<code>systemctl --user status deal-scout.service</code>"
            )

        # Force=True to bypass quiet hours so critical health checks always deliver
        send_alert(msg, force=True)
        print(f"[{now}] Health check sent: {'ONLINE' if is_active else 'OFFLINE'}")

    except Exception as e:
        print(f"Health check failed: {e}")

if __name__ == "__main__":
    check_service()
