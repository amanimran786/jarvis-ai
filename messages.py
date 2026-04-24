"""
iMessage / SMS sending via macOS Messages app using AppleScript.
Looks up contacts by name from the Contacts app.
"""

import subprocess
import re

_AMBIGUOUS_CONTACT = "__AMBIGUOUS_CONTACT__"
_FUZZY_MATCHES = "__FUZZY_MATCHES__"
_CONTACT_WITHOUT_HANDLE = "__CONTACT_WITHOUT_HANDLE__"

# Module-level store populated whenever a fuzzy search is triggered
_last_fuzzy_matches: list[str] = []
_last_contact_choices: list[dict[str, str]] = []
_last_applescript_error = ""


def _normalize_contact_label(label: str) -> str:
    text = (label or "").strip()
    match = re.fullmatch(r"_\$\!<(.+?)>\!\$_", text)
    if match:
        text = match.group(1)
    text = text.replace("_", " ").replace("-", " ").strip()
    return re.sub(r"\s+", " ", text).lower()


def _contact_handle_descriptor(kind: str, label: str) -> str:
    normalized_kind = (kind or "").strip().lower()
    normalized_label = _normalize_contact_label(label)
    parts = [part for part in (normalized_label, normalized_kind) if part]
    return " ".join(parts).strip()


def _run_applescript(script: str) -> tuple[str, str]:
    """Run AppleScript, return (stdout, stderr)."""
    global _last_applescript_error
    _last_applescript_error = ""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10
        )
    except subprocess.TimeoutExpired:
        _last_applescript_error = "macOS Contacts or Messages took too long to respond."
        return "", _last_applescript_error
    stderr = result.stderr.strip()
    if stderr:
        _last_applescript_error = stderr
    return result.stdout.strip(), stderr


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
        safe_word = word.replace('"', '\\"')
        for field in ("name", "nickname"):
            script = f"""
            tell application "Contacts"
                set matches to (every person whose {field} contains "{safe_word}")
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


def _mask_contact_handle(address: str) -> str:
    address = (address or "").strip()
    if not address:
        return "no handle"
    if "@" in address:
        local, _, domain = address.partition("@")
        return f"{local[:2]}***@{domain}" if local else f"***@{domain}"
    digits = re.sub(r"\D", "", address)
    if len(digits) >= 4:
        return f"ending {digits[-4:]}"
    return address


def _format_contact_choice(name: str, address: str, label: str = "", kind: str = "") -> str:
    masked = _mask_contact_handle(address)
    descriptor = _contact_handle_descriptor(kind, label)
    detail = ", ".join(part for part in (descriptor, masked) if part)
    return f"{name} ({detail})" if detail else name


def _normalize_contact_address(address: str) -> str:
    value = (address or "").strip()
    if not value:
        return ""
    if "@" in value:
        return value
    return re.sub(r"[\s\-\(\)]", "", value)


def _dedupe_choice_displays(choices: list[dict[str, str]]) -> list[dict[str, str]]:
    counts: dict[str, int] = {}
    for choice in choices:
        counts[choice["display"]] = counts.get(choice["display"], 0) + 1
    seen: dict[str, int] = {}
    for choice in choices:
        display = choice["display"]
        if counts[display] <= 1:
            continue
        seen[display] = seen.get(display, 0) + 1
        choice["display"] = f"{display} [contact {seen[display]}]"
    return choices


def _contact_search_specs(name: str) -> list[tuple[str, str, str]]:
    specs: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    full = name.strip()
    first_token = full.split()[0].strip() if full else ""
    candidates = [
        ("name", "is", full),
        ("name", "contains", first_token),
        ("nickname", "contains", full),
        ("nickname", "contains", first_token),
    ]
    for field, comparator, term in candidates:
        if not term:
            continue
        spec = (field, comparator, term)
        if spec not in seen:
            seen.add(spec)
            specs.append(spec)
    return specs


def _collect_contact_rows(name: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for field, comparator, search_term in _contact_search_specs(name):
        script = f"""
        tell application "Contacts"
            set matches to (every person whose {field} {comparator} "{search_term}")
            if (count of matches) is 0 then
                return ""
            end if
            set outText to ""
            repeat with p in matches
                repeat with ph in phones of p
                    set outText to outText & (name of p) & tab & "phone" & tab & (label of ph as string) & tab & (value of ph as string) & linefeed
                end repeat
                repeat with em in emails of p
                    set outText to outText & (name of p) & tab & "email" & tab & (label of em as string) & tab & (value of em as string) & linefeed
                end repeat
            end repeat
            return outText
        end tell
        """
        out, _err = _run_applescript(script)
        if not out:
            continue
        seen_keys = {
            (
                row["name"],
                row["kind"],
                row["label"],
                row["value"],
            )
            for row in rows
        }
        for line in out.splitlines():
            parts = [part.strip() for part in line.split("\t")]
            if len(parts) != 4:
                continue
            entry = {
                "name": parts[0],
                "kind": parts[1].lower(),
                "label": _normalize_contact_label(parts[2]),
                "value": parts[3],
            }
            key = (entry["name"], entry["kind"], entry["label"], entry["value"])
            if key not in seen_keys:
                seen_keys.add(key)
                rows.append(entry)
        if rows:
            return rows
    return rows


def get_last_contact_options() -> list[str]:
    if _last_contact_choices:
        return [choice["display"] for choice in _last_contact_choices]
    return list(_last_fuzzy_matches)


def resolve_last_contact_selection(selection: str) -> str | None:
    text = (selection or "").strip()
    lower = text.lower()
    if _last_contact_choices:
        number_match = re.fullmatch(r"(?:option\s*)?(\d+)", lower)
        if number_match:
            index = int(number_match.group(1)) - 1
            if 0 <= index < len(_last_contact_choices):
                return _last_contact_choices[index]["address"]
        for choice in _last_contact_choices:
            if lower == choice["display"].lower():
                return choice["address"]
    for name in _last_fuzzy_matches:
        if lower == name.lower() or lower in name.lower():
            return name
    return None


def lookup_contact(name: str) -> str | None:
    """
    Look up a contact's phone number or email by name.
    Returns:
      - a phone/email string on unambiguous exact or first-name match
      - _AMBIGUOUS_CONTACT  if more than one contact matches with handle info stored
      - _FUZZY_MATCHES      if no match at all (fuzzy suggestions stored in _last_fuzzy_matches)
      - None                if no match AND no fuzzy suggestions found
    """
    global _last_fuzzy_matches, _last_contact_choices

    for field, comparator, search_term in _contact_search_specs(name):
        script = f"""
        tell application "Contacts"
            set matches to (every person whose {field} {comparator} "{search_term}")
            if (count of matches) is 0 then
                return ""
            end if
            if (count of matches) is greater than 1 then
                -- Return contact names plus their first handle so duplicate names
                -- become selectable later without exposing the full number here.
                set nameList to "__MULTI__"
                repeat with p in matches
                    set chosenHandle to ""
                    set chosenKind to ""
                    set chosenLabel to ""
                    if (count of phones of p) > 0 then
                        set chosenKind to "phone"
                        set chosenLabel to label of item 1 of phones of p as string
                        set chosenHandle to value of item 1 of phones of p
                    else if (count of emails of p) > 0 then
                        set chosenKind to "email"
                        set chosenLabel to label of item 1 of emails of p as string
                        set chosenHandle to value of item 1 of emails of p
                    end if
                    if chosenHandle is not "" then
                        set nameList to nameList & linefeed & (name of p) & tab & chosenKind & tab & chosenLabel & tab & chosenHandle
                    else
                        set nameList to nameList & linefeed & (name of p)
                    end if
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
            # Multiple matches — extract choices and store them for the caller.
            choices = []
            for line in out.splitlines()[1:]:
                if not line.strip():
                    continue
                parts = [part.strip() for part in line.split("\t")]
                if len(parts) == 4:
                    choice_name, choice_kind, choice_label, choice_address = parts
                elif len(parts) == 2:
                    choice_name, choice_address = parts
                    choice_kind, choice_label = "", ""
                else:
                    choice_name, choice_address = line.strip(), ""
                    choice_kind, choice_label = "", ""
                normalized_address = _normalize_contact_address(choice_address)
                display = _format_contact_choice(choice_name, normalized_address, choice_label, choice_kind)
                choices.append({
                    "name": choice_name,
                    "address": normalized_address,
                    "display": display,
                    "label": _normalize_contact_label(choice_label),
                    "kind": (choice_kind or "").strip().lower(),
                })
            reachable_choices = [choice for choice in choices if choice["address"]]
            if len(reachable_choices) == 1:
                _last_contact_choices = []
                _last_fuzzy_matches = []
                return reachable_choices[0]["address"]
            if not reachable_choices:
                _last_contact_choices = []
                _last_fuzzy_matches = []
                return _CONTACT_WITHOUT_HANDLE
            _last_contact_choices = _dedupe_choice_displays(reachable_choices[:5])
            _last_fuzzy_matches = [choice["display"] for choice in _last_contact_choices]
            return _AMBIGUOUS_CONTACT

        if out:
            cleaned = _normalize_contact_address(out)
            if cleaned:
                _last_contact_choices = []
                _last_fuzzy_matches = []
                return cleaned

    # No match at all — try fuzzy search
    fuzzy = list_contacts_fuzzy(name)
    if fuzzy:
        _last_contact_choices = []
        _last_fuzzy_matches = fuzzy
        return _FUZZY_MATCHES

    _last_contact_choices = []
    _last_fuzzy_matches = []
    return None


def describe_contact_handles(name: str) -> str:
    rows = _collect_contact_rows(name)
    if not rows:
        if _last_applescript_error:
            return f"I couldn't read contact handles for {name}. {_last_applescript_error}"
        fuzzy = list_contacts_fuzzy(name)
        if fuzzy:
            names_str = _format_name_list(fuzzy, "or")
            return f"I couldn't find exact contact handles for {name}. Did you mean: {names_str}?"
        return f"I couldn't find contact handles for {name}."

    lines = []
    for row in rows[:10]:
        descriptor = _contact_handle_descriptor(row["kind"], row["label"]) or row["kind"] or "contact handle"
        lines.append(f"{row['name']}: {descriptor} {row['value']}")
    intro = f"Here are the contact handles I found for {name}:"
    return intro + "\n- " + "\n- ".join(lines)


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
            options = get_last_contact_options()
            if options:
                numbered = "; ".join(f"{idx + 1}. {option}" for idx, option in enumerate(options))
                return (
                    f"I found multiple contacts for {recipient}: {numbered}. "
                    f"Reply with option 1, option 2, or the exact contact label."
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

        if found == _CONTACT_WITHOUT_HANDLE:
            return (
                f"I found contacts named {recipient}, but none of them have a phone number or email in Contacts. "
                f"Add one and try again."
            )

        if not found:
            if _last_applescript_error:
                return f"I couldn't read Contacts for {recipient}. {_last_applescript_error}"
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
