---
description: Resume work from handoff document with context analysis and validation
---

Resume work from handoff through interactive analysis. Handoffs contain critical context, learnings, next steps from previous sessions requiring full understanding before continuation.

**Parameter Handling:** If handoff path provided, immediately read document and linked research/plans under `thoughts/shared/plans` or `thoughts/shared/research` — no sub-agents for critical files. If ticket number (ENG-XXXX) provided, list `thoughts/shared/handoffs/ENG-XXXX/` contents. Zero files or missing directory: "Can't find handoff, please provide path." One file: proceed. Multiple files: use most recent by YYYY-MM-DD_HH-MM timestamp. No parameters: respond with "I'll help resume from handoff. Which would you like? Tip: /resume_handoff path or /resume_handoff ENG-XXXX"

**Next Session Prompt Priority:** After reading handoff, check for `next_session_prompt:` field. If exists, present directly: "Previous session left prompt: > {contents} Shall I proceed or adjust approach?" If user approves, execute as-is using rest of handoff for context. Skip analysis steps — they're for handoffs lacking direct prompts.

**Full Analysis (when no next_session_prompt):** Read handoff completely without limit/offset. Spawn focused research tasks verifying current state — gather artifact context from feature docs, implementation plans, research documents mentioned. Wait for ALL sub-tasks before proceeding. Read critical files from learnings and recent changes sections.

**Present Synthesis:** "Analyzed handoff from [date]. Current situation: Original Tasks: [task]: [handoff status] → [current verification]. Key Learnings Validated: [learning with file:line] - [still valid/changed]. Recent Changes Status: [change] - [verified/missing/modified]. Artifacts Reviewed: [doc]: [takeaway]. Recommended Next Actions: [logical steps from handoff]. Potential Issues: [conflicts/regressions]. Shall I proceed with [action] or adjust?"

**Resume Autonomous Session:** After presenting synthesis and getting confirmation, check for an existing `/autonomous` session in `continuum/autonomous/`. If one exists with pending milestones, resume it — the handoff provides updated context and mental model, `/autonomous` drives execution. If no autonomous session exists, offer to start one using the handoff's `next:` steps and `next_session_prompt:` as the task specification.

**Validation Approach:** Be thorough — read entire handoff, verify ALL mentioned changes exist, check for regressions/conflicts, read referenced artifacts. Be interactive — present findings before starting, get buy-in on approach, allow course corrections, adapt based on current vs handoff state. Leverage handoff wisdom — pay attention to learnings section, apply documented patterns, avoid mentioned mistakes, build on discovered solutions.

**State Verification:** Never assume handoff state matches current. Verify file references exist, check for breaking changes since handoff, confirm patterns still valid. Handle scenarios: clean continuation (proceed with recommendations), diverged codebase (reconcile differences, adapt plan), incomplete work (complete unfinished first), stale handoff (re-evaluate strategy given major changes).
