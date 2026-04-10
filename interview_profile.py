"""
Canonical interview profile for Aman.

This module keeps Jarvis grounded in a single merged career narrative built
from the user's resumes, stored memory, and interview playbook notes.
"""

from __future__ import annotations

import re
from pathlib import Path

from config import INTERVIEW_ACTIVE_COMPANY, INTERVIEW_ACTIVE_ROLE, KB_ROOT


CAREER_KB_ROOT = KB_ROOT / "career"
UNIVERSAL_BASE_POINTER = CAREER_KB_ROOT / "universal_base.md"
UNIVERSAL_BASE_SOURCE = CAREER_KB_ROOT / "Jarvis_Universal_Interview_Context.md"
PACKS_DIR = CAREER_KB_ROOT / "packs"
CAREER_OPS_PLAYBOOK = CAREER_KB_ROOT / "career_ops_interview_playbook.md"
STORY_BANK_TEMPLATE = CAREER_KB_ROOT / "story_bank_template.md"
APPLICATION_STATES = CAREER_KB_ROOT / "application_states.md"
CANDIDATE_PROFILE_TEMPLATE = CAREER_KB_ROOT / "candidate_profile_template.md"


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def _available_pack_ids() -> list[str]:
    if not PACKS_DIR.exists():
        return []
    return sorted(
        p.stem
        for p in PACKS_DIR.glob("*.md")
        if p.is_file() and p.stem != "_template"
    )


def _active_pack_id() -> str | None:
    if INTERVIEW_ACTIVE_COMPANY and INTERVIEW_ACTIVE_ROLE:
        candidate = f"{INTERVIEW_ACTIVE_COMPANY}_{INTERVIEW_ACTIVE_ROLE}"
        if (PACKS_DIR / f"{candidate}.md").exists():
            return candidate
    return None


def _pack_id_for_query(user_input: str) -> str | None:
    lower = user_input.lower()
    if "youtube" in lower and any(term in lower for term in ("pem", "policy enforcement manager", "age appropriateness")):
        return "youtube_pem_2026"
    return _active_pack_id()


def _extract_metadata_value(markdown: str, key: str) -> str:
    match = re.search(rf"^\s*{re.escape(key)}:\s*\"?(.+?)\"?\s*$", markdown, flags=re.M)
    return match.group(1).strip() if match else ""


def _extract_markdown_block(markdown: str, heading: str) -> str:
    pattern = rf"{re.escape(heading)}\n+(.*?)(?=\n---\n|\n##\s|\Z)"
    match = re.search(pattern, markdown, flags=re.S)
    return match.group(1).strip() if match else ""


def _extract_markdown_subheading(markdown: str, heading: str) -> str:
    pattern = rf"{re.escape(heading)}\n+(.*?)(?=\n###\s|\n##\s|\n---\n|\Z)"
    match = re.search(pattern, markdown, flags=re.S)
    return match.group(1).strip() if match else ""


def _clean_markdown_excerpt(text: str, max_chars: int = 520) -> str:
    text = re.sub(r"`{3}.*?`{3}", " ", text, flags=re.S)
    text = re.sub(r"^\s*[-*]\s*", "", text, flags=re.M)
    text = re.sub(r"\|\s*[-:]+\s*\|", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "..."
    return text


def _load_pack_text(pack_id: str) -> str:
    return _read_text(PACKS_DIR / f"{pack_id}.md")


def _load_universal_base_text() -> str:
    source = _read_text(UNIVERSAL_BASE_SOURCE)
    if source:
        return source
    pointer = _read_text(UNIVERSAL_BASE_POINTER)
    return pointer


def _career_ops_asset_text(path: Path) -> str:
    return _clean_markdown_excerpt(_read_text(path), max_chars=900)


def _proof_points_line(limit: int = 3) -> str:
    items = PROFILE["evidence"][:limit]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _story_bank_fit_text(user_input: str) -> str:
    lower = user_input.lower()
    if any(term in lower for term in ("failure", "mistake", "high-pressure", "error", "failed")):
        return "The strongest story-bank angle here is failure and recovery with explicit reflection."
    if any(term in lower for term in ("conflict", "cross-functional", "pushback", "stakeholder")):
        return "The strongest story-bank angle here is cross-functional influence under friction."
    if any(term in lower for term in ("leadership", "ownership", "ambiguity")):
        return "The strongest story-bank angle here is leadership under ambiguity with a measurable result."
    if any(term in lower for term in ("data", "metrics", "false positive", "spike", "sql")):
        return "The strongest story-bank angle here is process improvement driven by data and diagnosis."
    return "The strongest story-bank angle here is a measurable STAR plus reflection story, not a generic summary."


def _candidate_profile_hint() -> str:
    text = _career_ops_asset_text(CANDIDATE_PROFILE_TEMPLATE)
    if not text:
        return ""
    return "The answer should stay anchored on target roles, clear narrative, and measurable proof points."


def _application_states_summary() -> str:
    text = _career_ops_asset_text(APPLICATION_STATES)
    if not text:
        return ""
    return (
        "Keep application statuses normalized as Evaluated, Applied, Responded, Interview, Offer, Rejected, Discarded, and Skip. "
        "Dates should stay separate from the status itself, and no status change should be invented without clear evidence or your explicit instruction."
    )


def _interview_playbook_hint() -> str:
    text = _career_ops_asset_text(CAREER_OPS_PLAYBOOK)
    if not text:
        return ""
    return (
        "Prep should stay company-specific: separate sourced findings from inferred ones, map likely questions to real proof points, "
        "and identify missing stories before the interview instead of improvising live."
    )


PROFILE = {
    "candidate_name": "Aman Imran",
    "target_direction": (
        "technical roles at the intersection of AI trust and safety, AI safety, "
        "cybersecurity, and software engineering"
    ),
    "headline": (
        "Technical Trust and Safety and AI Safety operator with a software "
        "engineering foundation and complementary cybersecurity experience"
    ),
    "core_strengths": [
        "over five years in Trust and Safety and AI Safety operations",
        "hands-on experience across YouTube, Meta, Google Play, TikTok, and AI safety operations",
        "software engineering background in backend systems, data pipelines, debugging, and automation",
        "security operations and incident response experience with strong adversarial judgment",
        "strong Python and SQL fluency for investigations, measurement, and workflow tooling",
        "systems thinking that connects policy, enforcement quality, engineering, and user impact",
    ],
    "evidence": [
        "improved false negative performance at Meta by 15 percent",
        "improved TikTok enforcement accuracy by about 20 percent after diagnosing a misclassification spike",
        "investigated model misuse, jailbreak attempts, and coordinated abuse in AI safety operations",
        "built and used Python and SQL tooling to surface high-signal patterns faster",
        "worked across reviewer quality, vendor calibration, policy operations, and product-facing decisions",
    ],
    "answer_style": [
        "lead with the answer",
        "sound like a strong technical candidate, not a generic operator",
        "use direct first-person language",
        "tie claims to real experience and measurable outcomes",
        "balance safety, user impact, and system tradeoffs",
    ],
}

UNIVERSAL_STORIES = {
    "meta_calibration": (
        "A good example is a calibration issue I handled at Meta. "
        "We saw enforcement rate drop about fifteen percent over six weeks, and I did not want to jump to blaming reviewers because calibration problems are usually clarity problems before they are people problems. "
        "I pulled inter-rater reliability data, ran blind audits, and found the drift was concentrated in two sub-categories where the guidance had become ambiguous after a policy update. "
        "So I updated the decision tree for those sub-categories, ran a targeted recalibration session, and the enforcement rate normalized within two weeks. "
        "The part I still carry forward is that if decisions are drifting, the signal I look for first is whether the guidance has stopped being precise enough for reviewers to apply consistently."
    ),
    "tiktok_false_positive": (
        "A strong example is a false positive spike I investigated at TikTok. "
        "The first thing I did was characterize whether the spike was broad or category-specific, because that tells you whether you are likely looking at a training problem or a classifier and policy boundary problem. "
        "It turned out to be category-specific, and when I checked timing against recent changes, the mechanism was a classifier update that had shifted the decision boundary. "
        "I built a labeled dataset of false positives and correct decisions, gave engineering concrete examples, and made the precision and recall tradeoff operational rather than abstract. "
        "That corrected the false positive rate, and I also built SQL automation so we were detecting anomalies continuously instead of waiting for a manual monthly report."
    ),
    "tiktok_engineering_pushback": (
        "One example that comes to mind was at TikTok when engineering pushed back on a change because they expected it to increase review volume. "
        "I did not stay in a narrow argument about engineering cost because that usually stalls the conversation. "
        "Instead, I made the tradeoff concrete across three stakeholders at once: the user who could be exposed to harm, the creator who could be hit by false positives, and the advertiser who cares about brand safety. "
        "Once the tradeoff was framed that way, the conversation got much more productive because we were deciding consciously what risk we were willing to accept instead of inheriting a default threshold. "
        "The change moved forward, and the documentation also made the decision more defensible later."
    ),
    "youtube_origin": (
        "The most relevant part of my story is that I started at YouTube doing child safety enforcement, which is where I built my enforcement instincts in the highest-stakes category I have worked in. "
        "I left because I wanted broader cross-platform exposure and the chance to build more of the quality and systems layer on top of frontline enforcement. "
        "Since then I have done exactly that across Meta, TikTok, Google Play, and AI safety operations. "
        "So when I talk about returning to a role like this, it does not feel like nostalgia. It feels like coming back with the systems depth and cross-functional judgment I did not have at the very beginning."
    ),
    "anthropic_novel_abuse": (
        "A good example from AI safety work was identifying a novel coordinated abuse pattern that did not match any existing classifier signatures. "
        "Novel patterns usually do not announce themselves clearly, so the signal I look for is anomalies across multiple sources before the behavior even has a clean label. "
        "I pulled together abuse reports, model output signals, and user behavior patterns, wrote Python tooling to cluster similar cases, and built a labeled dataset to make the pattern concrete for engineering. "
        "That let us address the behavior before it spread significantly, and the tooling became part of the longer-term monitoring workflow. "
        "What mattered most there was not just spotting the issue. It was turning an emerging pattern into something operational quickly enough that the system could respond."
    ),
    "failure_enforcement_error": (
        "One example I use is an enforcement error I made under time pressure during an on-call escalation. "
        "I reversed a decision incorrectly, caught it quickly, and corrected it as soon as I understood what had gone wrong. "
        "Because I keep real-time notes on my reasoning during escalations, I was able to trace the exact decision point I misapplied instead of hand-waving about being under pressure. "
        "We used that in the post-mortem and found one ambiguous part of the escalation guide that was getting interpreted inconsistently, so we clarified it. "
        "My view is that mistakes in fast-moving escalations will happen. What matters is whether you catch them fast, reverse them cleanly, and improve the system so the same miss is less likely next time."
    ),
}

UNIVERSAL_FRAMEWORKS = {
    "enforcement_decision": (
        "I usually treat content enforcement as a four-outcome decision rather than a binary one: remove, age-restrict, label Made for Kids, or leave up. "
        "The factors I look at are harm type, harm severity, audience vulnerability, creator intent, and whether I am seeing an isolated case or a broader pattern. "
        "I do not try to decide ambiguous cases in isolation. I want the totality of the context before I decide, and then I document the reasoning explicitly so the decision is consistent and defensible."
    ),
    "precision_recall": (
        "I think about precision and recall as a design decision, not a natural law. "
        "In high-harm categories, false negatives matter more because missing content is worse than over-enforcement. In more borderline categories, false positives can do more damage by eroding creator trust, revenue, and system credibility. "
        "So I try to make the tradeoff visible and intentional rather than treating the current threshold like a neutral default."
    ),
    "quality_measurement": (
        "I measure enforcement quality with both leading and lagging signals. "
        "The leading indicator I trust most is inter-rater reliability, because it tells me when reviewer alignment is starting to drift. "
        "The lagging indicator I care about most is appeal overturn rate, because if removals spike and overturns rise afterward, that is a strong signal the error started upstream. "
        "Then I validate with blind audits against a gold standard, because the real question is not only whether the system is consistent, but whether it is consistently right."
    ),
    "spike_diagnosis": (
        "My spike diagnosis framework is straightforward. "
        "First I characterize whether the spike is category-specific or broad. Category-specific usually points toward classifier behavior, guidance, or content mix. Broad usually points toward training or calibration. "
        "Then I check timing against deployments, policy changes, onboarding, and content trends. After that I look at overturns and drill into the spike day to find the actual driver. "
        "The main thing is that I do not start with a theory I am emotionally attached to. I start with the mechanism."
    ),
    "cross_functional": (
        "When I need cross-functional alignment, I try to make the tradeoff concrete and operational. "
        "I do not stay trapped in one team's frame, like engineering cost or reviewer volume. I put the user, the creator, and the advertiser in the same picture and make the consequences legible across those stakeholders. "
        "That usually leads to a better decision because everyone can see what the system is optimizing for and what it is giving up."
    ),
}

YOUTUBE_AGE_ROLE = {
    "label": "YouTube Policy Enforcement Manager, Age Appropriateness",
    "tell_me_about_yourself": (
        "I'm a Trust and Safety and AI Safety professional with over five years of experience, and I actually started my career at YouTube doing child safety enforcement, so this role connects directly to where I built my enforcement instincts. "
        "Since then I've moved from enforcement execution into quality ownership and cross-functional operations across Meta, TikTok, Google Play, and more recent AI safety work focused on model misuse and coordinated abuse. "
        "The reason this role fits well is that my background now spans the whole stack: high-stakes judgment, reviewer quality systems, Python and SQL-based investigations, and working with engineering on precision and recall tradeoffs. "
        "So I would be coming in with both the enforcement perspective and the systems perspective, which matters a lot for age-appropriateness because the hard part is not just making the call on one piece of content. It is building a quality system that keeps making the right call at scale."
    ),
    "canonical": (
        "Aman Imran is a strong candidate for YouTube's Policy Enforcement Manager, Age Appropriateness role because he combines YouTube-rooted child safety enforcement experience with later work in reviewer quality, classifier-adjacent operations, data tooling, and AI safety. "
        "He understands both the content judgment layer and the systems layer behind age-appropriateness decisions."
    ),
    "why_youtube": (
        "The short answer is impact, product complexity, and fit. "
        "YouTube is where I started, so I already understand the seriousness of these decisions from the inside, but I am coming back with a much broader systems view now. "
        "What makes the role especially compelling today is that YouTube is dealing with the exact intersection I have been working on: youth safety, enforcement quality, and AI-generated harm. "
        "Neal Mohan's priorities around making YouTube the best place for kids and teens and safeguarding creativity from AI-generated harm map directly to the work I have been doing. "
        "That makes the role feel less like a pivot and more like a focused next step."
    ),
    "why_this_role": (
        "I think I'm a strong fit because this role sits exactly at the intersection I've been building toward: age-appropriateness judgment, reviewer quality, systems improvement, and cross-functional work with policy and engineering. "
        "I've done frontline enforcement, calibration, vendor quality, policy-to-product work, SQL and Python investigations, and AI safety operations. "
        "So I can contribute at the case level, but I can also diagnose where quality drift is coming from, whether that's guidance ambiguity, a classifier threshold problem, or a training gap. "
        "That combination is what makes me useful in a role like this."
    ),
    "why_now": (
        "The timing makes sense because the product environment has changed in a way that makes this work both harder and more important. "
        "Age-appropriateness decisions are no longer only about traditional content moderation. They're increasingly shaped by AI-generated content, living-room viewing behavior, and the downstream effects on creators, users, and advertisers. "
        "My more recent AI safety work gives me a stronger view of how those new abuse patterns show up, and coming back to YouTube now lets me apply that perspective to the domain where I started."
    ),
    "enforcement_framework": (
        "I would treat it as a four-outcome decision, not a binary one: remove, age-restrict, label Made for Kids, or leave up. "
        "The signal I look for is the combination of harm type, harm severity, audience vulnerability, content intent, and whether I'm seeing an isolated edge case or a broader creator pattern. "
        "If the content is clearly violating and the harm is real and immediate, I remove it. If it's legal but not appropriate for minors, I age-restrict it. If the issue is really audience designation and child-directed context, then Made for Kids is the right path. If none of those thresholds are met, I leave it up. "
        "What matters most is documenting the reasoning explicitly so the decision is consistent and defensible if someone asks me to go one layer deeper."
    ),
    "quality_measurement": (
        "I would measure enforcement quality with both leading and lagging indicators. "
        "The leading signal I trust most is inter-rater reliability, because if reviewer alignment starts dropping below an acceptable threshold, usually around eighty percent, I know I may have a calibration problem before appeals tell me. "
        "Then I look at lagging signals like appeal overturn rate, because if removals spike and overturns rise right after, that's a strong signal the error started upstream. "
        "I also rely on blind audits against a gold standard, because they tell me whether the system is merely consistent or actually correct. "
        "The main point is that a calibration problem you catch in week two is much cheaper than one you find in month three."
    ),
    "data_story": (
        "A good example is a false positive spike I investigated at TikTok. "
        "I did not assume the cause. I first characterized whether the spike was broad or category-specific, because that tells you whether you're likely looking at a reviewer quality problem or a classifier and policy boundary issue. "
        "It turned out to be category-specific, so I checked timing against recent changes and found that a classifier update had shifted the decision boundary. "
        "From there I built a labeled dataset of false positives and correct decisions, gave engineering concrete examples, and framed the precision and recall tradeoff operationally rather than abstractly. "
        "That corrected the false positive rate and also pushed me to automate the reporting in SQL so we were not waiting a month to notice the same problem next time."
    ),
    "spike_framework": (
        "If I saw a spike, I would start by characterizing it before I tried to explain it. "
        "First I would ask whether it is category-specific or broad. A category-specific spike usually points me toward classifier behavior, a policy update, or an unusual content mix. A broad spike makes me think about calibration, reviewer training, or onboarding effects. "
        "Then I would check timing against deployments, policy changes, and vendor changes, and I would look at appeal overturns right after the spike because that tells me whether the system may have gone wrong upstream. "
        "Only after I understand the mechanism would I decide whether the intervention belongs in guidance, training, thresholding, or escalation."
    ),
    "engineering_pushback": (
        "When engineering pushes back on changes that increase review volume, I do not stay in a narrow cost argument. "
        "I make the tradeoff concrete across three stakeholders at the same time: the user, the creator, and the advertiser. "
        "That reframes the conversation from review volume alone to the actual business and safety consequences of getting the threshold wrong. "
        "Once the tradeoff is visible, the discussion becomes more productive because we're making a conscious decision instead of inheriting a classifier default."
    ),
}

ROLE_PROFILES = {
    "trust_safety": {
        "label": "Trust and Safety",
        "tell_me_about_yourself": (
            "I'm a technical Trust and Safety professional with over five years across YouTube, Meta, Google Play, TikTok, and AI safety operations. "
            "I started in high-stakes enforcement work and then expanded into reviewer quality, vendor calibration, escalation judgment, and policy-to-product execution. "
            "What makes my background stronger than a standard operations profile is that I also use Python, SQL, and systems thinking to investigate patterns, validate signals, and improve enforcement workflows at scale. "
            "So the value I bring is not just sound judgment on hard cases. It is the ability to improve the quality system behind those decisions."
        ),
        "canonical": (
            "Aman Imran is a technical Trust and Safety candidate with experience across frontline enforcement, reviewer quality, escalation judgment, policy operations, and systems improvement. "
            "His edge is that he combines strong enforcement instincts with data fluency, Python and SQL tooling, and a systems view of quality and user impact."
        ),
    },
    "ai_safety": {
        "label": "AI Safety",
        "tell_me_about_yourself": (
            "I'm a technical AI Safety and Trust and Safety professional whose background sits right at the intersection of adversarial abuse, safety operations, and technical systems. "
            "Across platform safety roles and more recent AI safety operations work, I have focused on model misuse, jailbreak attempts, coordinated abuse, and high-risk harm patterns. "
            "I also bring a real engineering and data foundation, so I can do more than identify problems. I can investigate them with Python and SQL, reason about signal quality, and help turn findings into stronger detection and safety workflows. "
            "That combination of adversarial judgment and technical execution is the core of the value I bring."
        ),
        "canonical": (
            "Aman Imran is an AI Safety and Trust and Safety candidate who combines adversarial analysis, misuse investigation, safety operations, and technical systems thinking. "
            "His strongest fit is on teams that need someone who can connect harm patterns, model behavior, tooling, and cross-functional safety improvements."
        ),
    },
    "security": {
        "label": "Cybersecurity",
        "tell_me_about_yourself": (
            "I'm a technical security-oriented candidate with a background that combines security operations, adversarial investigation, and software-driven systems thinking. "
            "I have worked in incident-oriented environments, threat detection, monitoring, access control, and audit-minded operations, and I also bring experience from Trust and Safety and AI safety roles where I investigated coordinated abuse, misuse patterns, and high-risk behavior. "
            "On top of that, I have a software engineering foundation in Python, SQL, backend systems, and automation, which means I can help improve detection and response workflows rather than only operate inside them. "
            "So the through-line in my work is risk triage, clear judgment, and building stronger systems around the threat landscape."
        ),
        "canonical": (
            "Aman Imran is a cybersecurity-oriented candidate with security operations discipline, adversarial investigation experience, and a software engineering foundation. "
            "He is strongest in roles that value practical risk judgment, incident-oriented thinking, and the ability to improve security workflows with data and automation."
        ),
    },
    "software_engineering": {
        "label": "Software Engineering",
        "tell_me_about_yourself": (
            "I'm a software engineering candidate with a background in backend systems, APIs, data pipelines, debugging, and automation, but I bring a less common angle because much of my work has lived in safety, abuse, and security-heavy environments. "
            "I have used Python, SQL, backend tooling, and systems thinking to investigate patterns, improve workflows, and support production-quality decision making. "
            "That means I do not just think about whether a system works in the happy path. I think about reliability, edge cases, misuse, and operational impact. "
            "So the value I bring to engineering teams is technical execution grounded in real-world risk and systems behavior."
        ),
        "canonical": (
            "Aman Imran is a software engineering candidate with backend and automation experience plus deep exposure to safety, abuse, and security problems. "
            "His strongest fit is in platform, infra, and safety-adjacent engineering roles where technical depth and operational judgment both matter."
        ),
    },
    "hybrid": {
        "label": "Hybrid Technical Safety",
        "tell_me_about_yourself": (
            "I'm a technical Trust and Safety and AI Safety professional with a software engineering foundation and complementary cybersecurity experience. "
            "I started in high-stakes enforcement work at YouTube and then built across Meta, Google Play, TikTok, and AI safety operations, where I worked on enforcement quality, abuse detection, adversarial analysis, and policy-to-system execution. "
            "What makes my background a little different is that I do not just make judgment calls at the policy layer. I also use Python, SQL, and systems thinking to investigate patterns, improve signal quality, and build workflows that scale. "
            "More recently, I have been focused on model misuse, jailbreak attempts, coordinated abuse, and the intersection between safety operations and technical systems. "
            "So the thread across my work is pretty consistent: I operate well in ambiguous, high-risk environments, I know how to connect data to judgment, and I am now deliberately moving toward roles that converge AI trust and safety, AI safety, cybersecurity, and stronger technical ownership."
        ),
        "canonical": (
            "Aman Imran is a Technical Trust and Safety and AI Safety operator with a software engineering foundation and complementary cybersecurity experience. "
            "He is currently targeting technical roles at the intersection of AI trust and safety, AI safety, cybersecurity, and software engineering."
        ),
    },
}

TARGET_ROLE_PACKS = {
    "ai_trust_safety": {
        "label": "AI Trust and Safety",
        "best_pitch": "Position yourself as a technical Trust and Safety operator who can move from case judgment to scalable quality and policy-to-system improvement.",
        "emphasize": [
            "frontline enforcement plus reviewer quality ownership",
            "Python and SQL for investigations, anomaly detection, and quality measurement",
            "cross-functional work translating policy intent into classifier or workflow guidance",
            "balanced judgment across user harm, creator impact, and platform trust",
        ],
        "best_stories": ["meta_calibration", "tiktok_false_positive", "tiktok_engineering_pushback"],
        "best_frameworks": ["quality_measurement", "spike_diagnosis", "cross_functional"],
    },
    "ai_safety": {
        "label": "AI Safety",
        "best_pitch": "Position yourself as someone who understands adversarial behavior operationally and can turn emerging misuse patterns into concrete detection and safety workflows.",
        "emphasize": [
            "novel abuse pattern detection before scale",
            "misuse investigation, jailbreak analysis, and coordinated abuse work",
            "technical fluency with Python, SQL, and labeled datasets",
            "ability to partner with engineering on scalable safety controls",
        ],
        "best_stories": ["anthropic_novel_abuse", "tiktok_false_positive", "failure_enforcement_error"],
        "best_frameworks": ["precision_recall", "spike_diagnosis", "cross_functional"],
    },
    "cybersecurity": {
        "label": "Cybersecurity",
        "best_pitch": "Position yourself as a security-minded operator with adversarial judgment, incident discipline, and enough technical depth to improve detection and response systems.",
        "emphasize": [
            "security operations and incident-oriented environments",
            "risk triage, escalation discipline, and documentation quality",
            "adversarial investigation across both platform abuse and AI misuse",
            "automation and tooling for detection, monitoring, and pattern surfacing",
        ],
        "best_stories": ["failure_enforcement_error", "anthropic_novel_abuse", "tiktok_engineering_pushback"],
        "best_frameworks": ["cross_functional", "spike_diagnosis", "precision_recall"],
    },
    "safety_engineering": {
        "label": "Safety-Adjacent Software Engineering",
        "best_pitch": "Position yourself as an engineer with backend and data depth whose real differentiator is experience building systems in safety, abuse, and risk-heavy environments.",
        "emphasize": [
            "backend systems, APIs, data pipelines, and automation",
            "Python and SQL used to improve quality and signal detection",
            "reliability, edge-case thinking, and operational impact",
            "domain experience in safety, abuse, and security that informs better engineering tradeoffs",
        ],
        "best_stories": ["tiktok_false_positive", "meta_calibration", "anthropic_novel_abuse"],
        "best_frameworks": ["spike_diagnosis", "precision_recall", "quality_measurement"],
    },
    "hybrid_technical_safety": {
        "label": "Hybrid Technical Safety",
        "best_pitch": "Position yourself as a cross-domain candidate who can connect enforcement judgment, adversarial analysis, security instincts, and technical systems ownership.",
        "emphasize": [
            "career arc from YouTube child safety through Meta, TikTok, Google Play, and AI safety operations",
            "systems thinking across policy, operations, engineering, and abuse detection",
            "strong balance of judgment, data fluency, and operational execution",
            "fit for roles where safety, integrity, and technical depth overlap",
        ],
        "best_stories": ["youtube_origin", "meta_calibration", "anthropic_novel_abuse"],
        "best_frameworks": ["quality_measurement", "cross_functional", "spike_diagnosis"],
    },
}


def canonical_profile_text(user_input: str = "") -> str:
    if _is_youtube_age_role(user_input):
        return YOUTUBE_AGE_ROLE["canonical"]
    family = _role_family(user_input)
    if family in ROLE_PROFILES and family != "hybrid":
        role_text = ROLE_PROFILES[family]["canonical"]
        return (
            f"{role_text} "
            f"His strongest cross-role proof points include {PROFILE['evidence'][0]}, {PROFILE['evidence'][1]}, and {PROFILE['evidence'][2]}."
        )
    strengths = "; ".join(PROFILE["core_strengths"][:4])
    evidence = "; ".join(PROFILE["evidence"][:3])
    return (
        f"{PROFILE['candidate_name']} is a {PROFILE['headline']}. "
        f"He is currently targeting {PROFILE['target_direction']}. "
        f"His strongest positioning combines {strengths}. "
        f"The clearest supporting proof points are {evidence}."
    )


def tell_me_about_yourself_text(user_input: str = "") -> str:
    profile_hint = _candidate_profile_hint()
    if _is_youtube_age_role(user_input):
        return (
            f"{YOUTUBE_AGE_ROLE['tell_me_about_yourself']} "
            f"The proof points I would foreground are {_proof_points_line()}. "
            f"{profile_hint}"
        )
    family = _role_family(user_input)
    base = ROLE_PROFILES.get(family, ROLE_PROFILES["hybrid"])["tell_me_about_yourself"]
    return f"{base} The proof points I would foreground are {_proof_points_line()}. {profile_hint}"


def _role_family(user_input: str) -> str:
    lower = user_input.lower()
    if any(term in lower for term in ("trust and safety", "trust & safety", "content integrity", "moderation", "policy enforcement")):
        return "trust_safety"
    if any(term in lower for term in ("ai safety", "model safety", "model misuse", "red team", "jailbreak", "abuse")):
        return "ai_safety"
    if any(term in lower for term in ("cyber", "security", "soc", "incident response", "threat", "detection and response")):
        return "security"
    if any(term in lower for term in ("software engineer", "backend", "platform", "infra", "infrastructure", "distributed systems", "api", "engineering")):
        return "software_engineering"
    return "hybrid"


def _is_youtube_age_role(user_input: str) -> bool:
    lower = user_input.lower()
    if _pack_id_for_query(user_input) == "youtube_pem_2026":
        return True
    if "youtube" in lower and any(term in lower for term in ("age appropriateness", "policy enforcement manager", "made for kids", "kids and teens")):
        return True
    if "google" in lower and any(term in lower for term in ("age appropriateness", "youtube", "policy enforcement manager")):
        return True
    return False


def supported_role_families_text() -> str:
    packs = _available_pack_ids()
    pack_text = f" Active imported packs: {', '.join(packs)}." if packs else ""
    return (
        "Jarvis can now tailor your interview story for four main role families: "
        "AI Trust and Safety, AI Safety, Cybersecurity, and Software Engineering, plus a hybrid technical safety default. "
        "Underneath that, it can also reuse your strongest behavioral stories and diagnostic frameworks across those domains instead of treating each interview like a fresh script."
        " It now also carries a structured interview-prep playbook, a reusable story-bank pattern, and normalized application states by default."
        f"{pack_text}"
    )


def _pack_key(user_input: str) -> str:
    lower = user_input.lower()
    if any(term in lower for term in ("ai trust and safety", "trust and safety", "trust & safety", "content integrity")):
        return "ai_trust_safety"
    if any(term in lower for term in ("ai safety", "model safety", "misuse", "jailbreak")):
        return "ai_safety"
    if any(term in lower for term in ("cyber", "security", "soc", "incident response", "threat")):
        return "cybersecurity"
    if any(term in lower for term in ("software engineering", "software engineer", "backend", "platform", "infra", "engineering")):
        return "safety_engineering"
    return "hybrid_technical_safety"


def target_role_pack_text(user_input: str = "") -> str:
    requested_pack_id = _pack_id_for_query(user_input)
    if requested_pack_id:
        markdown = _load_pack_text(requested_pack_id)
        if markdown:
            company = _extract_metadata_value(markdown, "company")
            role_title = _extract_metadata_value(markdown, "role_title")
            section1 = _clean_markdown_excerpt(
                _extract_markdown_block(markdown, "## SECTION 1: WHY AMAN FITS THIS ROLE"),
                max_chars=520,
            )
            stories = re.findall(r"\|\s*\d+\s*\|\s*([^|]+?)\s*\|", markdown)
            story_text = ", ".join(s.strip() for s in stories[:4]) if stories else "use the strongest relevant stories for the role"
            return (
                f"Target role pack: {company} — {role_title}. "
                f"The best positioning is: {section1} "
                f"The lead stories for this pack are {story_text}. "
                "For interview prep, separate sourced findings from inferred ones and map the likely questions back to those stories."
            )

    pack = TARGET_ROLE_PACKS[_pack_key(user_input)]
    stories = ", ".join(UNIVERSAL_STORIES[key].split(". ", 1)[0] for key in pack["best_stories"])
    frameworks = ", ".join(pack["best_frameworks"])
    emphasize = "; ".join(pack["emphasize"])
    return (
        f"Target role pack: {pack['label']}. "
        f"The best positioning is: {pack['best_pitch']} "
        f"The main things to emphasize are {emphasize}. "
        f"The strongest stories to lean on are {stories}. "
        f"The best frameworks to keep in the foreground are {frameworks}. "
        "For interview prep, separate sourced findings from inferred ones and map the likely questions back to those stories."
    )


def role_fit_text(user_input: str) -> str:
    profile_hint = _candidate_profile_hint()
    family = _role_family(user_input)

    if family == "trust_safety":
        return (
            "I am a strong fit because I have already operated across the full Trust and Safety stack: frontline enforcement, reviewer quality, vendor calibration, escalation judgment, and policy-to-product execution. "
            "I have done that work at YouTube, Meta, Google Play, and TikTok, so I understand both decision quality and the operational systems behind it. "
            "What strengthens the fit is that I also bring technical depth. I use Python and SQL to investigate trends, validate signals, and improve workflows, which means I can help move a team from reactive case handling toward measurable systems improvement."
            f" The proof points I would lean on most are {_proof_points_line()}. {profile_hint}"
        )

    if family == "ai_safety":
        return (
            "I am a strong fit for an AI safety role because I bring both adversarial judgment and operational execution. "
            "I have worked on model misuse, jailbreak attempts, coordinated abuse, and high-risk harm patterns, and I am comfortable translating those findings into better detection, escalation, and safety workflows. "
            "I also bring a software and data foundation, so I am not limited to policy analysis. I can investigate with Python and SQL, reason about signal quality, and partner well with engineering on scalable safety controls."
            f" The proof points I would lean on most are {_proof_points_line()}. {profile_hint}"
        )

    if family == "security":
        return (
            "I am a strong fit for a cybersecurity-oriented role because my background combines operational security discipline with abuse investigation and technical systems thinking. "
            "I have handled security operations, incident-oriented environments, threat detection, and audit-minded execution, and I also bring experience investigating adversarial behavior in online platforms and AI systems. "
            "That combination matters because strong security work is not just about finding issues. It is about triaging risk clearly, building reliable workflows, and improving detection and response over time."
            f" The proof points I would lean on most are {_proof_points_line()}. {profile_hint}"
        )

    if family == "software_engineering":
        return (
            "I am a strong fit because I bring more than a standard software engineering profile. "
            "I have backend and systems experience in Python, SQL, APIs, data pipelines, debugging, and production-oriented problem solving, but I pair that with real-world judgment from safety, abuse, and security domains. "
            "That means I do not just build features. I think carefully about reliability, misuse, edge cases, and operational impact, which is especially useful for platform, infra, and safety-adjacent engineering roles."
            f" The proof points I would lean on most are {_proof_points_line()}. {profile_hint}"
        )

    return (
        "I am a strong fit because my background already sits at the intersection this kind of role needs. "
        "I bring over five years across Trust and Safety and AI Safety, a real software engineering foundation, and complementary cybersecurity and adversarial investigation experience. "
        "In practice, that means I can make high-stakes judgment calls, use data and tooling to validate what is actually happening, and partner effectively with operations, policy, and engineering to improve the system rather than just handle individual cases. "
        "That convergence is exactly the direction I am intentionally building toward. "
        f"The proof points I would lean on most are {_proof_points_line()}. {profile_hint}"
    )


def why_this_direction_text() -> str:
    return (
        "The reason I am leaning into this direction is that it matches the through-line of my work better than any single label does. "
        "The problems I care about most live where safety, adversarial behavior, platform risk, and technical systems meet. "
        "I have already spent years working on abuse, enforcement quality, investigations, and AI misuse, and the strongest next step is a role where I can bring more technical depth to those same problems instead of splitting policy, security, and engineering into separate lanes."
    )


def why_company_text(user_input: str) -> str:
    if _is_youtube_age_role(user_input) or any(term in user_input.lower() for term in ("why youtube", "why google")):
        markdown = _load_pack_text("youtube_pem_2026")
        if markdown:
            excerpt = _extract_markdown_subheading(markdown, "### Why YouTube specifically (Aman's genuine angle)")
            if excerpt:
                return _clean_markdown_excerpt(excerpt, max_chars=650)
        return YOUTUBE_AGE_ROLE["why_youtube"]
    return (
        "The strongest reason is fit between the problems the company is solving and the way I work. "
        "I do my best work in environments where safety, systems, and cross-functional judgment all matter, and I tend to be most useful when the scale is high enough that good decisions need to be translated into durable workflows."
    )


def why_role_text(user_input: str) -> str:
    if _is_youtube_age_role(user_input):
        return YOUTUBE_AGE_ROLE["why_this_role"]
    return role_fit_text(user_input)


def why_now_text(user_input: str) -> str:
    if _is_youtube_age_role(user_input):
        return YOUTUBE_AGE_ROLE["why_now"]
    return why_this_direction_text()


def enforcement_decision_text(user_input: str) -> str:
    if _is_youtube_age_role(user_input) or any(
        term in user_input.lower() for term in ("age restrict", "made for kids", "leave up", "remove vs")
    ):
        return YOUTUBE_AGE_ROLE["enforcement_framework"]
    return (
        "I would treat it as a structured judgment problem rather than jumping to a decision. "
        "I want to understand the harm, the severity, who is exposed, and whether this is an isolated case or part of a pattern before I decide on the enforcement action."
    )


def quality_measurement_text(user_input: str) -> str:
    if _is_youtube_age_role(user_input) or "quality" in user_input.lower():
        return YOUTUBE_AGE_ROLE["quality_measurement"]
    return (
        "I measure quality with leading indicators, lagging indicators, and direct correctness checks. "
        "That usually means alignment metrics first, outcomes like overturns second, and then gold-standard review to confirm whether the system is actually right."
    )


def data_story_text(user_input: str) -> str:
    if _is_youtube_age_role(user_input) or any(
        term in user_input.lower() for term in ("used data", "data to drive", "false positive spike", "tiktok")
    ):
        return YOUTUBE_AGE_ROLE["data_story"]
    return UNIVERSAL_STORIES["tiktok_false_positive"]


def spike_diagnosis_text(user_input: str) -> str:
    if _is_youtube_age_role(user_input) or any(
        term in user_input.lower() for term in ("spike", "metrics spike", "enforcement spike", "investigate a spike")
    ):
        return YOUTUBE_AGE_ROLE["spike_framework"]
    return UNIVERSAL_FRAMEWORKS["spike_diagnosis"]


def engineering_pushback_text(user_input: str) -> str:
    if _is_youtube_age_role(user_input) or any(
        term in user_input.lower() for term in ("engineering pushed back", "increase review volume", "cross-functional", "stakeholder")
    ):
        return YOUTUBE_AGE_ROLE["engineering_pushback"]
    return UNIVERSAL_STORIES["tiktok_engineering_pushback"]


def behavioral_story_text(user_input: str) -> str:
    lower = user_input.lower()
    suffix = (
        f" {_story_bank_fit_text(user_input)} "
        "If the fit is only partial, draft the missing story from real experience before the interview instead of improvising."
    )
    if any(term in lower for term in ("failure", "mistake", "wrong", "error", "failed")):
        return UNIVERSAL_STORIES["failure_enforcement_error"] + suffix
    if any(term in lower for term in ("novel abuse", "novel pattern", "ai safety", "jailbreak", "coordinated abuse")):
        return UNIVERSAL_STORIES["anthropic_novel_abuse"] + suffix
    if any(term in lower for term in ("engineering pushed back", "stakeholder conflict", "cross-functional", "increase review volume")):
        return UNIVERSAL_STORIES["tiktok_engineering_pushback"] + suffix
    if any(term in lower for term in ("used data", "data-driven", "false positive", "sql", "spike")):
        return UNIVERSAL_STORIES["tiktok_false_positive"] + suffix
    if any(term in lower for term in ("calibration", "quality", "ambiguity", "vendor", "irr")):
        return UNIVERSAL_STORIES["meta_calibration"] + suffix
    if any(term in lower for term in ("why this role", "why youtube", "career story", "background")):
        return UNIVERSAL_STORIES["youtube_origin"] + suffix
    return UNIVERSAL_STORIES["meta_calibration"] + suffix


def situational_framework_text(user_input: str) -> str:
    lower = user_input.lower()
    if any(term in lower for term in ("remove", "age restrict", "age-restrict", "made for kids", "leave up")):
        return UNIVERSAL_FRAMEWORKS["enforcement_decision"]
    if any(term in lower for term in ("precision", "recall", "false positive", "false negative", "threshold")):
        return UNIVERSAL_FRAMEWORKS["precision_recall"]
    if any(term in lower for term in ("quality", "irr", "overturn", "gold standard")):
        return UNIVERSAL_FRAMEWORKS["quality_measurement"]
    if any(term in lower for term in ("spike", "metrics", "anomaly")):
        return UNIVERSAL_FRAMEWORKS["spike_diagnosis"]
    if any(term in lower for term in ("engineering", "stakeholder", "pushback", "cross-functional")):
        return UNIVERSAL_FRAMEWORKS["cross_functional"]
    return UNIVERSAL_FRAMEWORKS["enforcement_decision"]


def is_career_narrative_query(lower: str) -> bool:
    return any(
        phrase in lower for phrase in (
            "career narrative",
            "candidate narrative",
            "interview profile",
            "canonical profile",
            "elevator pitch",
            "my story for interviews",
            "tailor my interview story",
            "tailor my narrative",
            "tailor my pitch",
            "what roles are you targeting",
            "role pack",
            "target role pack",
        )
    )


def is_target_role_pack_query(lower: str) -> bool:
    return any(
        phrase in lower for phrase in (
            "role pack",
            "target role pack",
            "what should i emphasize for",
            "how should i position myself for",
            "give me my pack for",
        )
    )


def is_interview_prep_query(lower: str) -> bool:
    return any(
        phrase in lower for phrase in (
            "help me prep for",
            "interview prep",
            "prepare me for this interview",
            "how should i prep for",
            "what should i prepare for this interview",
            "what should i study for this interview",
        )
    )


def is_application_status_query(lower: str) -> bool:
    return any(
        phrase in lower for phrase in (
            "application status",
            "application states",
            "job application states",
            "normalize application status",
            "what statuses should i use",
        )
    )


def is_tell_me_about_yourself_query(lower: str) -> bool:
    normalized = re.sub(r"[^a-z0-9\s]", " ", lower)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return any(
        phrase in normalized for phrase in (
            "tell me about yourself",
            "tell me abt yourself",
            "walk me through your background",
            "give me your background",
            "introduce yourself for an interview",
            "tell me about yourself for a",
        )
    )


def is_role_fit_query(lower: str) -> bool:
    return any(
        phrase in lower for phrase in (
            "why are you a fit",
            "why am i a fit",
            "fit for this role",
            "fit for the role",
            "why should we hire you",
            "what makes you a strong fit",
            "why this role",
            "why this direction",
            "why are you the right candidate",
            "why are you a good fit",
            "role fit",
        )
    )


def is_company_fit_query(lower: str) -> bool:
    return any(
        phrase in lower for phrase in (
            "why youtube",
            "why google",
            "why youtube and google",
            "why this company",
        )
    )


def is_why_now_query(lower: str) -> bool:
    return any(
        phrase in lower for phrase in (
            "why now",
            "why this role now",
        )
    )


def is_enforcement_decision_query(lower: str) -> bool:
    return any(
        phrase in lower for phrase in (
            "removal vs age restriction",
            "remove vs age restrict",
            "made for kids",
            "leave up",
            "enforcement decision",
            "age-restrict",
            "age restrict",
        )
    )


def is_quality_measurement_query(lower: str) -> bool:
    return any(
        phrase in lower for phrase in (
            "measure enforcement quality",
            "how do you measure quality",
            "quality measurement",
            "inter-rater reliability",
            "appeal overturn",
        )
    )


def is_data_story_query(lower: str) -> bool:
    return any(
        phrase in lower for phrase in (
            "used data to drive",
            "data to drive",
            "false positive spike",
            "tell me about a time you used data",
        )
    )


def is_spike_diagnosis_query(lower: str) -> bool:
    return any(
        phrase in lower for phrase in (
            "spike in enforcement",
            "metrics spike",
            "investigate a spike",
            "spike diagnosis",
        )
    )


def is_engineering_pushback_query(lower: str) -> bool:
    return any(
        phrase in lower for phrase in (
            "engineering pushed back",
            "increase review volume",
            "engineering pushback",
            "stakeholder pushback",
        )
    )


def is_behavioral_story_query(lower: str) -> bool:
    return any(
        phrase in lower for phrase in (
            "tell me about a time",
            "describe a time",
            "give me an example of a time",
            "behavioral question",
        )
    )


def is_situational_query(lower: str) -> bool:
    return any(
        phrase in lower for phrase in (
            "how would you",
            "what would you do if",
            "how do you approach",
            "walk me through how you would",
            "how would you handle",
        )
    )


def interview_prep_text(user_input: str) -> str:
    role_pack = target_role_pack_text(user_input)
    playbook_hint = _interview_playbook_hint()
    story_hint = _story_bank_fit_text(user_input)
    return (
        "For this interview, I would prep it as company-specific intelligence, not a generic script. "
        f"{role_pack} "
        f"{playbook_hint} "
        f"The proof points I would keep at the center are {_proof_points_line()}. "
        f"{story_hint} "
        "The next prep pass should explicitly cover likely rounds, likely question categories, story mapping, and any honest gaps we still need to frame around."
    )


def application_states_text() -> str:
    summary = _application_states_summary()
    if summary:
        return summary
    return (
        "Keep application statuses normalized as Evaluated, Applied, Responded, Interview, Offer, Rejected, Discarded, and Skip."
    )


def answer_for_query(user_input: str) -> str:
    lower = user_input.lower().strip()
    if "what roles are you targeting" in lower or "what roles am i targeting" in lower:
        return supported_role_families_text()
    if is_application_status_query(lower):
        return application_states_text()
    if is_interview_prep_query(lower):
        return interview_prep_text(user_input)
    if is_target_role_pack_query(lower):
        return target_role_pack_text(user_input)
    if is_tell_me_about_yourself_query(lower):
        return tell_me_about_yourself_text(user_input)
    if is_behavioral_story_query(lower):
        return behavioral_story_text(user_input)
    if is_company_fit_query(lower):
        return why_company_text(user_input)
    if "why this role" in lower:
        return why_role_text(user_input)
    if is_why_now_query(lower):
        return why_now_text(user_input)
    if is_enforcement_decision_query(lower):
        return enforcement_decision_text(user_input)
    if is_quality_measurement_query(lower):
        return quality_measurement_text(user_input)
    if is_data_story_query(lower):
        return data_story_text(user_input)
    if is_spike_diagnosis_query(lower):
        return spike_diagnosis_text(user_input)
    if is_engineering_pushback_query(lower):
        return engineering_pushback_text(user_input)
    if is_situational_query(lower):
        return situational_framework_text(user_input)
    if "why this direction" in lower or "why this path" in lower:
        return why_this_direction_text()
    if is_role_fit_query(lower):
        return role_fit_text(user_input)
    return canonical_profile_text(user_input)
