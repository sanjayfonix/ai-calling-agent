"""
Test script for Method 2: Twilio Voice Webhook
Simulates what your Express backend would do to initiate a call.
"""

from twilio.rest import Client
import os
from urllib.parse import urlencode
from dotenv import load_dotenv

load_dotenv()

# Twilio credentials
account_sid = os.getenv('TWILIO_ACCOUNT_SID')
auth_token = os.getenv('TWILIO_AUTH_TOKEN')
twilio_phone = os.getenv('TWILIO_PHONE_NUMBER')

client = Client(account_sid, auth_token)

# Agent information (would come from your Express backend database)
agent_data = {
    'agent_id': 8,
    'agent_name': 'Himanshu Mathis',
    'agent_email': 'himanshu.garg@fonixtech.io',
    'agent_phone': '+917073873731',
    'agent_npn': 'sccdcdcdcd',
    'agent_role': 'Agent',
    'plan_name': 'STARTER',
    'slots': '2026-03-05|09:00,2026-03-05|09:30,2026-03-05|10:00,2026-03-05|10:30,2026-03-05|11:00',
    'slots_count': 5
}

# Build the webhook URL with agent parameters (properly URL-encoded)
# This is what your Express backend would construct
base_url = "https://ai-calling-agent-wxdz.onrender.com/twilio/voice"
query_params = urlencode(agent_data)
webhook_url = f"{base_url}?{query_params}"

# Customer phone number
to_number = '+918949968414'

print("🔵 Initiating call via Method 2 (Twilio Voice Webhook)...")
print(f"📞 Calling: {to_number}")
print(f"🤖 Agent: {agent_data['agent_name']}")
print(f"🔗 Webhook URL: {webhook_url[:100]}...")

try:
    # This is the key difference from Method 1
    # Instead of passing TwiML directly, we give Twilio a URL to fetch TwiML from
    call = client.calls.create(
        to=to_number,
        from_=twilio_phone,
        url=webhook_url,  # Twilio will GET this URL to get TwiML instructions
        method='GET',
        status_callback=f"https://ai-calling-agent-wxdz.onrender.com/api/webhooks/call-status",
        status_callback_event=['initiated', 'ringing', 'answered', 'completed'],
        record=True,
        recording_channels='dual',
        recording_status_callback=f"https://ai-calling-agent-wxdz.onrender.com/api/webhooks/recording-status",
        recording_status_callback_event=['completed']
    )
    
    print(f"\n✅ Call initiated successfully!")
    print(f"📋 Call SID: {call.sid}")
    print(f"📊 Status: {call.status}")
    print(f"\n🎯 What happens next:")
    print(f"1. Twilio calls the customer at {to_number}")
    print(f"2. Twilio fetches TwiML from /twilio/voice endpoint")
    print(f"3. TwiML tells Twilio to connect audio to WebSocket")
    print(f"4. Sarah introduces herself on behalf of {agent_data['agent_name']}")
    print(f"5. Customer data sent to webhook after call completes")
    
except Exception as e:
    print(f"\n❌ Error: {str(e)}")
