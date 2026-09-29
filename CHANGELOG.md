# Changelog

## 1.6.0 — prepared 2026-09-29

- Reconcile archived child-start callbacks through verified session aliases so completed runs no longer leave another session's Stop blocked. Preserve checks for conflicting live owners and unknown starts.
- Clarify Codex root completion: the native Stop hook finalizes a completed run when the root turn ends. Native concurrency checks now record bounded Stop decisions and require gate release before lead completion.
- Add GPT-6.1 Sol to Codex routing. Preferred profiles use it for Sol work, while existing GPT-6 Sol profiles remain available to accounts without the new model.
- Add Claude Sonnet 5.5 to native account discovery and routing, and generate its packaged agents. Existing Sonnet 5 profiles remain available; Fable still requires explicit access.
- Refresh reviewed capability ranks and token rates for both providers. Keep entitlement checks and high-risk effort floors in the route validator.

Model availability is account, client, and workspace dependent. GPT-6.1 Sol supports `low` through `max` in the API; Codex can also expose `ultra` through its native roster. Claude Sonnet 5.5 supports `low` through `max`; Claude Code requires v2.1.284 or later for it. Live account entitlement and provider acceptance still require `refresh_profiles.py --verify` with suitable credentials.

Sources reviewed 2026-09-29: [OpenAI API model](https://developers.openai.com/api/docs/models/gpt-6.1-sol), [Codex models](https://learn.chatgpt.com/docs/models), [Codex pricing](https://learn.chatgpt.com/docs/pricing), [Claude Sonnet 5.5](https://platform.claude.com/docs/en/models/sonnet-5-5/overview), [Claude effort](https://platform.claude.com/docs/en/build-with-claude/effort), [Claude Code model configuration](https://code.claude.com/docs/en/model-config).
