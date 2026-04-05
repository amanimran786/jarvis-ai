"""
iMessage / SMS sending via macOS Messages app using AppleScript.
Also supports looking up contacts by name.
"""

import subprocess
import re


def _run_applescript(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=10
    )
    return result.stdout.strip()


def lookup_contact(name: str) -> str | None:
    """Look up a contact's phone number or email by name using Contacts app."""
    script = f"""
    tell application "Contacts"
        set matches to (people whose name contains "{name}")
        if length of matches > 0 then
            set p to item 1 of matches
            if (count of phones of p) > 0 then
                return value of item 1 of phones of p
            else if (count of emails of p) > 0 then
                return value of item 1 of emails of p
            end if
        end if
        return ""
    end tell
    """
    result = _run_applescript(script)
    # Clean phone number (remove spaces, dashes, parens)
    if result:
        cleaned = re.sub(r"[\s\-\(\)]", "", result)
        return cleaned if cleaned else None
    return None


def send_imessage(recipient: str, message: str) -> str:
    """
    Send an iMessage. recipient can be a name, phone number, or email.
    If a name is given, looks up the contact first.
    """
    # If it looks like a name (not a number/email), look up the contact
    is_address = re.search(r"[\d@\+]", recipient)
    address = recipient

    if not is_address:
        found = lookup_contact(recipient)
        if not found:
            return f"Could not find a contact named {recipient} in your address book."
        address = found

    # Escape quotes in message
    safe_msg = message.replace('"', '\\"')
    script = f"""
    tell application "Messages"
        set targetService to 1st account whose service type = iMessage
        set targetBuddy to participant "{address}" of targetService
        send "{safe_msg}" to targetBuddy
    end tell
    """
    try:
        _run_applescript(script)
        return f"Message sent to {recipient}."
    except Exception as e:
        return f"Failed to send message: {e}"


def open_conversation(name: str) -> str:
    """Open the Messages app to a specific conversation."""
    script = f"""
    tell application "Messages"
        activate
    end tell
    """
    _run_applescript(script)
    return f"Opened Messages app."
