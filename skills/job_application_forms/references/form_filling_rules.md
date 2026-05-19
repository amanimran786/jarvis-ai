Resume parsing:
- Extract only work experience facts that appear in the user-provided resume or in explicit user instructions.
- Treat "Present", "Current", or an open-ended date range as currently_work_here=true.
- If the resume gives only a year, leave the month blank or ask for the missing month when the form requires it.
- Do not infer location from company headquarters unless the resume states it.
- Build role descriptions from resume-backed bullets. Prefer measurable scope, tools, domain, and outcomes already present in the resume.

Form inspection:
- Before entry, list the visible experience blocks and whether each block is empty or already populated.
- Detect required markers such as asterisk labels, aria-required, required attributes, and validation text.
- Detect date expectations from labels, placeholders, helper text, input masks, and existing examples.
- Map each resume role to one form block before typing. Add new blocks only when the page clearly exposes an add-experience control and the user requested all roles.

Entry and verification:
- For text inputs and textareas, set the value through normal typing or a DOM setter that dispatches input, change, and blur events.
- For selects, comboboxes, date pickers, and checkboxes, prefer the page's native controls over raw DOM assignment when practical.
- After each block, read the field values back from the page and compare them with the intended values.
- Stop if any value is truncated, duplicated, garbled, placed in the wrong field, or rejected by validation.

Safety:
- Never submit, apply, continue, save-and-continue, or move past the review boundary without explicit action-time confirmation.
- If the destination was not explicitly requested, ask before entering sensitive personal data into a third-party site.
- If automation is blocked or unreliable, switch to the manual fallback format.
- For already populated fields, report the conflict and ask whether to preserve or replace.
