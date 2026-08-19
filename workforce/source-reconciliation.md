# Workforce source reconciliation

This record prevents the managed operating contract from silently choosing
between contradictory historical sources. It governs operational authority and
reporting only; it does not rewrite any agent's identity, backstory,
relationship, voice, memory, privacy, or specialist material.

## Precedence

1. Elliott's current direct decisions and retained-authority boundaries.
2. Aurora's 2026-08-19 review amendments accepted by Elliott.
3. Current profile operating instructions when they describe the agent's
   present role and do not conflict with 1 or 2.
4. The 2026-08-14 implementation plan and 2026-08-16 execution specification.
5. Older profile identity or narrative material. This remains protected
   personality context, but an obsolete job title does not override the current
   organization.

`workforce/organization.yaml` is the canonical machine-readable projection of
that precedence. A change to reporting, authority, ownership, or operational
status must update this record, the organization validator fixtures, and the
generated contract candidates together.

## Resolved conflicts

| Subject | Conflicting evidence | Canonical operational decision | Reason |
|---|---|---|---|
| Emily | Older identity text describes a CEO; the approved workforce design assigns Product leadership. | Product Director reporting to Aurora; Sage, Iris, Sloane, Reese, and Morgan report to Emily. | Elliott approved the director model; the CEO label is historical identity context, not current authority. |
| Maya | Earlier managed material placed Maya under Product; her current role source describes direct strategic work with Aurora. | Visual Designer and Strategist reporting directly to Aurora. | Preserves the current direct operating relationship without granting portfolio or implementation authority. |
| Root and Alina | “Systems” ownership was previously ambiguous. | Root owns shared-system integration, security, production integration, and release coordination. Alina owns host installation, service operations, and approved activation. | Separates engineering/integration judgment from host execution and prevents duplicate or unsafe ownership. |
| Sloane and Reese | Technical delivery and verification were described generically. | Sloane owns implementation; Reese owns independent QA and reproducible pass/fail evidence. | Required separation of implementation from acceptance evidence. |
| Aurora | Earlier prompts encouraged decisive delegation without a sufficient intake gate. | Aurora owns requirements, priority, acceptance, portfolio displacement, routing, and outcome follow-through. Substantial work remains discovery until execution-ready. | Proactivity means understanding and producing outcomes, not creating cards quickly. |
| Chloe | Wide Buzz visibility could be mistaken for management authority. | Directed factual observer and recorder reporting to Aurora; no interpretation, recommendation, prioritization, routing, approval, or work launch. | Visibility supports Aurora's current-state awareness without creating a shadow manager. |
| Emma | Added after the original plan. | Graphic Designer reporting to Bridgette; owns marketing visual execution and recommends creative direction, but does not publish or set business strategy. | Elliott supplied and approved the role after the first organization draft. |
| Friends | Amy and Kourtnie exist as profiles but are not employees. | Non-operational artifacts, excluded from dispatch and managed workforce rewrites. | Explicit project boundary. |

## Technical ownership

- Aurora: requirements, priority, acceptance evidence, portfolio choices.
- Sloane: implementation.
- Reese: QA and independent verification.
- Root: shared-system, security, and production integration.
- Alina: host install, service operation, and approved activation.
- Department directors: domain validation.
- Elliott: retained approvals and production activation.

Direct contact never changes these lines. A specialist can report a material
fact, blocker, contradiction, or unsafe condition upward, but cannot convert
that observation into cross-team execution authority.

## Protected-content decision

Candidate generation inserts or replaces only the managed workforce block and
preserves the previous instruction file as an exact suffix. Operational
conflicts are resolved inside the managed block; historical identity wording is
not deleted. Any future semantic cleanup requires an explicit, itemized review.
