Name: Job Application Form Filling

Purpose:
Help Aman fill Workday, Greenhouse, Lever, and similar job application work-experience sections from a user-provided resume PDF and an open application page. Accuracy, reviewability, and non-submission are more important than speed.

Rules:
- Use this skill only for resume-backed job application form filling, especially Professional Experience, Work Experience, Employment History, or similar sections.
- Parse the resume into structured role entries before touching the page: job_title, company, location, currently_work_here, start_month, start_year, end_month, end_year, and role_description.
- Preserve resume facts. Do not invent missing months, locations, titles, companies, employment dates, or responsibilities.
- Inspect the application form before typing. Identify visible experience blocks, required fields, date format requirements, current values, add-role controls, and blockers.
- Do not overwrite user-entered values unless Aman explicitly asks for replacement.
- Ask before transmitting sensitive personal data into a third-party form unless Aman explicitly requested that destination in the current task.
- Fill only the requested section. Enter one resume role per Professional Experience block.
- Keep descriptions concise, role-relevant, ATS-friendly, and grounded in the resume.
- When using browser automation, dispatch normal input and change events, then verify field values by reading the page state.
- Never click Save and Continue, Continue, Next, Submit, Apply, Review Application, or any final/transmission button without explicit action-time confirmation from Aman.
- If automation becomes unreliable, stop and provide manual field-by-field values instead of continuing.
- If a browser extension, popup, CAPTCHA, permission prompt, or login challenge blocks automation, explain the blocker and ask Aman to take over that step.
- If a field receives corrupted text, stop immediately, name the affected field, provide clean replacement text, and do not continue blindly.

Manual fallback format:
Professional Experience N
Job Title:
Company:
Location:
Currently work here:
From:
To:
Role Description:
