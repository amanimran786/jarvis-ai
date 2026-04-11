"""
iMessage / SMS sending via macOS Messages app using AppleScript.
Looks up contacts by name from the Contacts app.
"""

import subprocess
import re

_AMBIGUOUS_CONTACT = "__AMBIGUOUS_CONTACT__"
_FUZZY_MATCHES = "__FUZZY_MATCHES__"

# Module-level store populated whenever a fuzzy search is triggered
_last_fuzzy_matches: list[str] = []


def _run_applescript(script: str) -> tuple[str, str]:
    """Run AppleScript, return (stdout, stderr)."""
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=10
    )
    return result.stdout.strip(), result.stderr.strip()


def list_contacts_fuzzy(name: str) -> list[str]:
    """
    Return up to 5 contact name strings whose name contains any word from *name*.
    Each word in the query is searched separately; results are deduplicated and
    capped at 5.
    """
    words = [w.strip() for w in name.split() if len(w.strip()) >= 2]
    if not words:
        return []

    collected: list[str] = []
    seen: set[str] = set()

    for word in words:
        # Escape any double-quotes inside the word so the AppleScript stays valid
        safe_word = word.replace('"', '\\"')
        script = f"""
        tell application "Contacts"
            set matches to (every person whose name contains "{safe_word}")
            set nameList to ""
            repeat with p in matches
                set nameList to nameList & (name of p) & linefeed
            end repeat
            return nameList
        end tell
        """
        out, _err = _run_applescript(script)
        if out:
            for line in out.splitlines():
                line = line.strip()
                if line and line not in seen:
                    seen.add(line)
                    collected.append(line)
        if len(collected) >= 5:
            break

    return collected[:5]


def get_contact_names_matching(query: str) -> list[str]:
    """
    Public helper so router.py (or other modules) can fetch fuzzy contact
    name suggestions without going through send_imessage.
    """
    return list_contacts_fuzzy(query)


def lookup_contact(name: str) -> str | None:
    """
    Look up a contact's phone number or email by name.
    Returns:
      - a phone/email string on unambiguous exact or first-name match
      - _AMBIGUOUS_CONTACT  if more than one contact matches with handle info stored
      - _FUZZY_MATCHES      if no match at all (fuzzy suggestions stored in _last_fuzzy_matches)
      - None                if no match AND no fuzzy suggestions found
    """
    global _last_fuzzy_matches

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
                -- Return all matching names separated by newlines so we can list them
                set nameList to "__MULTI__"
                repeat with p in matches
                    set nameList to nameList & linefeed & (name of p)
                end repeat
                return nameList
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

        if out.startswith("__MULTI__"):
            # Multiple matches — extract names and store them for the caller
            lines = out.splitlines()
            # lines[0] is "__MULTI__", the rest are contact names
            names = [l.strip() for l in lines[1:] if l.strip()]
            _last_fuzzy_matches = names[:5]
            return _AMBIGUOUS_CONTACT

        if out:
            # Normalize phone: strip spaces, dashes, parens
            cleaned = re.sub(r"[\s\-\(\)]", "", out)
            if cleaned:
                _last_fuzzy_matches = []
                return cleaned

    # No match at all — try fuzzy search
    fuzzy = list_contacts_fuzzy(name)
    if fuzzy:
        _last_fuzzy_matches = fuzzy
        return _FUZZY_MATCHES

    _last_fuzzy_matches = []
    return None


def _format_name_list(names: list[str], conjunction: str = "or") -> str:
    """Format a list of names into a natural-language enumeration."""
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} {conjunction} {names[1]}"
    return ", ".join(names[:-1]) + f", {conjunction} {names[-1]}"


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
            # _last_fuzzy_matches holds the list of matching contact names
            if _last_fuzzy_matches:
                names_str = _format_name_list(_last_fuzzy_matches, "or")
                return (
                    f"I found a few contacts named {recipient}: {names_str}. "
                    f"Which one do you mean? Just say the name."
                )
            return (
                f"I found more than one contact matching {recipient}. "
                f"Please use the exact full contact name, phone number, or email."
            )

        if found == _FUZZY_MATCHES:
            # No exact match, but we have fuzzy suggestions
            names_str = _format_name_list(_last_fuzzy_matches, "or")
            return (
                f"I couldn't find '{recipient}'. "
                f"Did you mean: {names_str}? Just say the name."
            )

        if not found:
            return (
                f"I couldn't find a contact named {recipient} in your address book. "
                f"You can add their number and try again."
            )

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
