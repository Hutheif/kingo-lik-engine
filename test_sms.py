# test_sms.py — run once to confirm SMS works
# Usage: python test_sms.py

from dotenv import load_dotenv
load_dotenv()

import os
import africastalking

API_KEY     = os.environ.get("AT_API_KEY")
ALERT_PHONE = os.environ.get("ALERT_PHONE")

if not API_KEY:
    print("ERROR: AT_API_KEY not found in .env")
    exit(1)

if not ALERT_PHONE:
    print("ERROR: ALERT_PHONE not found in .env")
    print("Add this to your .env:  ALERT_PHONE=+254712345678")
    exit(1)

print("Testing SMS to:", ALERT_PHONE)
print("Using API key: ", API_KEY[:8] + "...")

africastalking.initialize(username="sandbox", api_key=API_KEY)

sms = africastalking.SMS

message = "KINGOLIK TEST ALERT. Phone: +254700000000. Keywords: violence, help. Message: Test alert - if you receive this, SMS is working."

try:
    response   = sms.send(message, [ALERT_PHONE], sender_id=None)
    recipients = response.get("SMSMessageData", {}).get("Recipients", [])

    if recipients:
        status = recipients[0].get("status", "unknown")
        cost   = recipients[0].get("cost", "unknown")
        print("\nResult:")
        print("  Status:", status)
        print("  Cost:  ", cost)
        print("  To:    ", ALERT_PHONE)
        if status == "Success":
            print("\n SMS working. Check your phone.")
        else:
            print("\n Status is not Success. Full response:", response)
    else:
        print("Unexpected response:", response)

except Exception as e:
    print("\nSMS failed:", e)