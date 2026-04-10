"""
iMessage / SMS sending via macOS Messages app using AppleScript.
Looks up contacts by name from the Contacts app.
"""

import subprocess
import re

_AMBIGUOUS_CONTACT = "__AMBIGUOUS_CONTACT__"


def _run_applescript(script: str) -> tuple[str, str]:
    """Run AppleScript, return (stdout, stderr)."""
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=10
    )
    return result.stdout.strip(), result.stderr.strip()


def lookup_contact(name: str) -> str | None:
    """
    Look up a contact's phone number or email by name.
    Requires a unique exact or near-exact match to avoid messaging the wrong person.
    """
    search_terms = [name.strip()]
    first_token = name.split()[0].strip() if name.strip() else ""
    if first_token and first_token.lower() != name.strip().lower():
        search_terms.append(first_token)

    for index, search_term in enumerate(search_terms):
        comparator = "is" if index == 0 else "contains"
        script = f"""
        tell application "Contacts"
            set matches to (every person whose name {comparator} "{search_term}")
            if (count of matches) is 0 then
                return ""
            end if
            if (count of matches) is greater than 1 then
                return "{_AMBIGUOUS_CONTACT}"
            end if
            set p to item 1 of matches
            if (count of phones of p) > 0 then
                return value of item 1 of phones of p
            else if (count of emails of p) > 0 then
                return value of item 1 of emails of p
            end if
        end tell
        return ""
        """
        out, err = _run_applescript(script)
        if out == _AMBIGUOUS_CONTACT:
            return _AMBIGUOUS_CONTACT
        if out:
            # Normalize phone: strip spaces, dashes, parens
            cleaned = re.sub(r"[\s\-\(\)]", "", out)
            if cleaned:
                return cleaned
    return None


def send_imessage(recipient: str, message: str) -> str:
    """
    Send an iMessage. recipient can be a name, phone number, or email.
    If a name is given, looks up the contact first.
    """
    # If it looks like a name (not a number/email), look up the contact
    is_address = bool(re.search(r"[\d@\+]", recipient))
    address = recipient

    if not is_address:
        found = lookup_contact(recipient)
        if found == _AMBIGUOUS_CONTACT:
            return (f"I found more than one contact matching {recipient}. "
                    f"Please use the exact full contact name, phone number, or email.")
        if not found:
            return (f"I couldn't find a contact named {recipient} in your address book. "
                    f"You can add their number and try again.")
        address = found

    safe_msg = message.replace('"', '\\"').replace("\\", "\\\\")

    script = f"""
    tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        set targetBuddy to buddy "{address}" of targetService
        send "{safe_msg}" to targetBuddy
    end tell
    """
    out, err = _run_applescript(script)

    if err and "execution error" in err.lower():
        # Fallback: try SMS service
        script_sms = f"""
        tell application "Messages"
            send "{safe_msg}" to buddy "{address}" of 1st service
        end tell
        """
        out2, err2 = _run_applescript(script_sms)
        if err2:
            return f"Could not send message to {recipient}. Error: {err2}"

    return f"Sent to {recipient}."
