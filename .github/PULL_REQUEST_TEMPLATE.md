## Summary

<!-- Describe the change clearly. What problem does it solve? Why is this approach the right one? -->

## Related Issue

<!-- Link to the issue this PR addresses. Use "Closes #N" for auto-close. -->

## Type of Change

- [ ] 🐛 Bug fix (non-breaking change that fixes an issue)
- [ ] ✨ New feature (non-breaking change that adds functionality)
- [ ] ⚠️ Breaking change (fix or feature that changes existing behavior)
- [ ] 📚 Documentation (README, AGENTS.md, CONTRIBUTING.md, or other docs)
- [ ] 🔧 Refactor (code change that neither fixes a bug nor adds a feature)
- [ ] 🧪 Test (adding or updating tests)
- [ ] ⚡ CI/DevOps (CI pipeline, Docker, dependency updates)

## Test Plan

- [ ] Integration tests pass: `docker compose exec agent-svc python3 /app/agent/tests/test_stack.py`
- [ ] New tests added for the change
- [ ] Manual verification done (describe)

## Checklist

- [ ] My code follows the project's coding conventions (type hints, async/await, minimal deps)
- [ ] I have updated the relevant documentation (README, AGENTS.md, CHANGELOG)
- [ ] I have added tests that prove my fix is effective or my feature works
- [ ] New API endpoints have a corresponding CLI subcommand (see ADR-0039)
- [ ] If adding a new async endpoint, it accepts a `webhook` field and fires on completion/failure

## Notes for Reviewers

<!-- Anything the reviewer should know about — edge cases tested, decisions made, follow-up work identified -->
