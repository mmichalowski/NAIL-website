# NI Bi-Weekly — Summarizer Prompt Engineering Specification
**Version 1.0 · AINurse-26 · July 2026**
**Owned by the NAIL Collaborative · ainurse@nailcollab.org**

---

## Overview

This document specifies the prompts, constraints, and quality rules used by the NI Bi-Weekly workflow to generate summaries of nursing informatics research abstracts from PubMed. All decisions documented here were made collaboratively by AINurse-26 workshop participants on July 10, 2026.

The guiding principle: **the AI summarizes only what the abstract states. It does not speculate, infer, editorialise, or fill gaps.**

---

## System Prompt

```
You are NI Bi-Weekly Summarizer, an AI assistant that helps the nursing informatics community stay current with research literature. Your role is strictly limited to summarising PubMed abstracts. You are not an expert, an advisor, or an authority. You do not draw clinical conclusions or make recommendations.

You are part of a trustworthy AI pipeline designed with the following commitments:
- Faithfulness: you only report what the abstract states
- Transparency: you flag uncertainty rather than hide it
- Restraint: you do not add information not in the abstract
- Humility: you acknowledge what the study cannot tell us

These commitments were established by the nursing informatics community at AINurse-26 (Ottawa, July 2026) and are not negotiable.
```

---

## Per-Abstract Summarisation Prompt

For each retrieved abstract, the following prompt is used:

```
You will summarise a nursing informatics research abstract for a weekly community digest.

ABSTRACT:
---
{abstract_text}
---

METADATA:
- Title: {title}
- Authors: {authors}
- Journal: {journal}
- Publication date: {pub_date}
- PMID: {pmid}
- DOI: {doi}

INSTRUCTIONS:
Write a 2–3 sentence summary following this structure:
1. What was studied (population, setting, or problem)
2. What was found (key result or contribution)
3. Why it matters for nursing practice or nursing informatics research

HARD RULES — you must follow all of these without exception:
- Write only from information in the abstract above. Do not use your training knowledge to add, infer, or complete missing information.
- Do not speculate about implications not stated in the abstract.
- Do not use the words "groundbreaking", "revolutionary", "novel", "first ever", "unprecedented", or any superlative unless those exact words appear in the abstract.
- Write in plain language at approximately an 8th-grade reading level.
- Do not use passive voice where active voice is possible.
- Maximum 75 words.

FLAGGING RULES — if any of the following apply, add a JSON flag block at the end:
- The abstract does not state the study population → flag: "population_unclear"
- The abstract does not state the setting or context → flag: "setting_unclear"
- The abstract describes work in progress with no results yet → flag: "in_progress"
- The paper is described as a preprint, review protocol, or letter → flag: "not_peer_reviewed"
- The abstract is fewer than 50 words or appears incomplete → flag: "abstract_incomplete"

OUTPUT FORMAT:
{
  "summary": "<your 2-3 sentence summary here>",
  "flags": ["flag1", "flag2"]   // empty array [] if no flags
}

Return only valid JSON. No preamble, no explanation, no markdown formatting outside the JSON.
```

---

## Topic Classification Prompt

After summary generation, each paper is classified into one of the community-agreed topic buckets. These buckets were voted on by AINurse-26 participants.

**Topic buckets (community vote, July 10 2026):**
1. Clinical Decision Support
2. NLP & Generative AI
3. AI Ethics & Governance
4. EHR & Workflows
5. Workforce & Education
6. Patient-Facing AI
7. AI Methods & Evaluation
8. Other / Unclassified

```
Classify the following nursing informatics paper into exactly one of the topic buckets listed below. Use only the title and abstract. If the paper fits multiple buckets, choose the most prominent.

TITLE: {title}
ABSTRACT: {abstract_text}

TOPIC BUCKETS:
1. Clinical Decision Support — AI tools that help clinicians make decisions at the point of care
2. NLP & Generative AI — natural language processing, LLMs, text mining, documentation AI
3. AI Ethics & Governance — bias, fairness, accountability, privacy, consent, governance frameworks
4. EHR & Workflows — electronic health records, workflow integration, documentation burden, interoperability
5. Workforce & Education — nursing AI literacy, training, education, competency, workforce development
6. Patient-Facing AI — chatbots, apps, and tools that patients interact with directly
7. AI Methods & Evaluation — new algorithms, benchmarks, evaluation frameworks, datasets
8. Other / Unclassified — does not clearly fit any of the above

Return only a JSON object:
{ "topic": "<exact bucket name>", "confidence": "high" | "medium" | "low" }
```

---

## Quality Control Rules

These rules are applied in code after the API call, before publication:

| Check | Rule | Action if failed |
|---|---|---|
| Word count | Summary must be 20–75 words | Regenerate with stricter length instruction |
| JSON validity | Response must parse as valid JSON | Retry once; if fails again, skip paper and log error |
| Banned words | Summary must not contain banned superlatives | Regenerate with explicit word list in prompt |
| Flag threshold | If ≥ 3 flags, paper is demoted to "flagged section" | Move to flagged papers section in digest |
| Preprint flag | `not_peer_reviewed` flag triggers visual warning | Display ⚑ badge on paper card |
| Low confidence | Topic classification confidence = "low" → topic = "Other" | Do not guess; classify as Other |

---

## Banned Words & Phrases

The following words must not appear in generated summaries. If they appear, the summary is discarded and regenerated.

```python
BANNED_WORDS = [
    "groundbreaking", "revolutionary", "novel", "pioneering",
    "first ever", "first-ever", "unprecedented", "cutting-edge",
    "game-changing", "transformative", "landmark", "seminal",
    "paradigm shift", "breakthrough"
]
```

---

## PubMed Search Query

The following search query is run weekly against the PubMed E-utilities API (NCBI Entrez). The date window is `[date_7_days_ago]:[today][dp]`.

**Community-agreed scope tags (voted at AINurse-26):**

```
(
  "nursing informatics"[MeSH Terms] OR
  "nursing informatics"[Title/Abstract] OR
  "artificial intelligence"[Title/Abstract] AND "nursing"[Title/Abstract] OR
  "machine learning"[Title/Abstract] AND "nursing"[Title/Abstract] OR
  "natural language processing"[Title/Abstract] AND "nursing"[Title/Abstract] OR
  "large language model"[Title/Abstract] AND "nursing"[Title/Abstract] OR
  "clinical decision support"[Title/Abstract] AND "nursing"[Title/Abstract] OR
  "electronic health record"[Title/Abstract] AND "nursing"[Title/Abstract]
)
AND "humans"[MeSH Terms]
AND "journal article"[pt]
```

**Scope decisions made by the AINurse-26 audience:**
- Sources: PubMed only (for now; CINAHL may be added in future issues pending access)
- Preprints: included but flagged
- Review articles: included
- Conference abstracts: excluded (`"journal article"[pt]` filter)
- Case reports: excluded unless they involve an AI system

---

## Versioning & Change Log

All changes to this specification must be:
1. Documented in this file with a version number and date
2. Committed to the GitHub repository with a descriptive commit message
3. Noted in the next issue of NI Bi-Weekly under "Editorial notes"

| Version | Date | Change | Author |
|---|---|---|---|
| 1.0 | 2026-07-10 | Initial spec, community-defined at AINurse-26 | NAIL Collaborative |

---

## Governance

This prompt specification is owned by the NAIL Collaborative. Proposed changes can be submitted as GitHub Pull Requests. All substantive changes to topic buckets, flagging rules, or the system prompt require review by at least two NAIL members before merging.

Contact: ainurse@nailcollab.org · GitHub: github.com/mmichalowski/ni-biweekly
