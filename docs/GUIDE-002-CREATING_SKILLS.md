# Creating Custom Skills

**Status:** ✅ Active  
**Last Updated:** October 2026  
**Type:** GUIDE (Developer Reference)

> Skills are markdown files that give Nova specialized knowledge for specific domains — coding conventions, deployment workflows, API patterns, or anything else you repeat often. This guide covers everything you need to write effective skills.

---

## How Skills Work

1. **Discovery** — Nova scans `~/.nova/skills/` at startup and builds a compact index of all skills (name + description)
2. **Indexing** — The index is injected into the system prompt as an XML block:
   ```xml
   <skills>
     Before replying, scan available skills. If any skill is even partially
     relevant, load it with skill_view(name) and follow its instructions.
     <category name="development">
       <skill name="python-coding">Python coding conventions, testing, and best practices</skill>
     </category>
   </skills>
   ```
3. **Loading** — When the agent identifies a relevant skill, it calls `skill_view("skill-name")` to load the full content
4. **Following** — The agent reads the skill's instructions and applies them to its response

Skills are **knowledge documents**, not executable code. They guide the agent's behavior through natural language instructions.

---

## File Structure

Each skill is a directory containing a single `SKILL.md` file:

```
~/.nova/skills/
└── my-skill/
    └── SKILL.md
```

The directory name is used as a fallback identifier if the `name` frontmatter field is missing.

---

## SKILL.md Format

```markdown
---
name: my-skill
category: development
description: One sentence describing what this skill does
---

# My Skill Title

The body of the skill goes here. Write it as instructions to the agent.
```

### Frontmatter fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Recommended | Identifier used in `skill_view(name)`. Defaults to directory name. |
| `category` | No | Groups skills in the index. Default: `"general"` |
| `description` | Recommended | One-line summary shown in the skills index. Keep it under 200 chars. |

Descriptions appear in every system prompt, so they are scanned for prompt-injection patterns. A flagged description is shown as `[description withheld: potential prompt injection]`.

---

## Creating a Skill

### Option 1: Manually

```bash
mkdir -p ~/.nova/skills/my-skill
cat > ~/.nova/skills/my-skill/SKILL.md << 'EOF'
---
name: my-skill
category: general
description: What this skill does in one sentence
---

# My Skill

Instructions go here.
EOF
```

### Option 2: Ask Nova to create it

```
You: Create a skill called "docker-workflow" in the development category that covers
     building images, running containers, and docker-compose patterns.
```

Nova will call `skill_manage(action="create", name="docker-workflow", ...)` to create it.

### Option 3: Use `skill_manage` directly

```
You: /tools
You: skill_manage(action="create", name="my-skill", category="general",
     description="My skill description", content="# My Skill\n\nInstructions here.")
```

---

## Writing Effective Skills

### Structure

A well-structured skill has:

1. **A clear title** — what domain this covers
2. **Conventions** — rules and patterns the agent should follow
3. **Common commands** — copy-pasteable commands for frequent tasks
4. **Pitfalls** — things to avoid or watch out for
5. **Examples** — concrete code or command examples

### Example: Docker Workflow Skill

```markdown
---
name: docker-workflow
category: devops
description: Docker image builds, container management, and docker-compose patterns
---

# Docker Workflow

## Building Images

Always tag images with a meaningful version:

```bash
docker build -t myapp:latest -t myapp:$(git rev-parse --short HEAD) .
```

Use multi-stage builds to keep images small:

```dockerfile
FROM python:3.13-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM python:3.13-slim
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY . .
CMD ["python", "-m", "myapp"]
```

## Running Containers

```bash
# Run with env file
docker run --env-file .env -p 8080:8080 myapp:latest

# Run interactively for debugging
docker run -it --rm myapp:latest /bin/bash

# Check logs
docker logs -f <container-id>
```

## Docker Compose

```bash
# Start all services
docker compose up -d

# Rebuild and restart a specific service
docker compose up -d --build api

# View logs for a service
docker compose logs -f api

# Stop and remove containers
docker compose down
```

## Pitfalls

- Never use `latest` as the only tag in production — always pin a version
- Don't copy `.env` files into images — use `--env-file` at runtime
- Add `.dockerignore` to exclude `.venv/`, `node_modules/`, `.git/`
- Use `--no-cache` when debugging build issues: `docker build --no-cache .`
```

### Example: API Integration Skill

```markdown
---
name: stripe-api
category: payments
description: Stripe API patterns for charges, subscriptions, and webhooks
---

# Stripe API

## Authentication

Always use the secret key from environment, never hardcode:

```python
import stripe
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
```

## Creating a Payment Intent

```python
intent = stripe.PaymentIntent.create(
    amount=2000,  # Amount in cents
    currency="usd",
    automatic_payment_methods={"enabled": True},
    metadata={"order_id": order_id},
)
```

## Handling Webhooks

Always verify the webhook signature:

```python
payload = request.body
sig_header = request.headers["Stripe-Signature"]
webhook_secret = os.environ["STRIPE_WEBHOOK_SECRET"]

try:
    event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
except stripe.error.SignatureVerificationError:
    return HttpResponse(status=400)
```

## Pitfalls

- Use idempotency keys for all write operations to prevent duplicate charges
- Always handle `stripe.error.CardError` separately from other exceptions
- Test with Stripe's test card numbers (4242 4242 4242 4242) before going live
- Webhook events can arrive out of order — always fetch the object from the API
```

---

## Skill Writing Tips

### Be directive, not descriptive

Write instructions the agent should follow, not descriptions of what the skill is about.

```markdown
# ❌ Descriptive (weak)
This skill covers Python testing. Python testing uses pytest.
Tests should be written in test files.

# ✅ Directive (strong)
## Testing Rules
- Use pytest for all tests — never unittest
- Name test files `test_*.py` and test functions `test_<what>_<when>`
- Run `pytest -v` before declaring any task complete
- If a test fails, fix the code — never skip or delete the test
```

### Include copy-pasteable commands

The agent will use these directly. Make them complete and correct:

```markdown
## Common Commands

```bash
# Create and activate a virtual environment
python3 -m venv .venv && source .venv/bin/activate

# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Run the full CI check
ruff check . && mypy . && pytest
```
```

### List pitfalls explicitly

The agent is more likely to avoid mistakes when they're called out:

```markdown
## Pitfalls

- Never commit secrets — use environment variables or `.env` files (gitignored)
- Don't use `os.system()` — use `subprocess.run()` with `check=True`
- Don't catch bare `Exception` — catch specific exception types
```

### Keep descriptions short and searchable

The description appears in the skills index that the agent scans on every turn. Make it specific enough to trigger loading when relevant:

```markdown
# ❌ Too vague
description: Coding help

# ✅ Specific and searchable
description: Python coding conventions, pytest testing, ruff linting, mypy type checking
```

### Use categories to group related skills

```
~/.nova/skills/
├── development/
│   ├── python-coding/SKILL.md
│   ├── typescript-coding/SKILL.md
│   └── git-workflow/SKILL.md
├── devops/
│   ├── docker-workflow/SKILL.md
│   └── kubernetes/SKILL.md
└── payments/
    └── stripe-api/SKILL.md
```

Categories appear in the skills index, helping the agent navigate large skill libraries.

---

## Managing Skills

### List all skills

```
/skills list
```

Or ask Nova: *"What skills do you have available?"* — it will call `skills_list`.

### View a skill

```
/skills view python-coding
```

Or ask Nova: *"Show me the python-coding skill."*

### Update a skill

Ask Nova to update it:

```
You: Update the docker-workflow skill to add a section on Docker BuildKit.
```

Nova will call `skill_manage(action="patch", name="docker-workflow", content="...")`.

Or edit the file directly:

```bash
$EDITOR ~/.nova/skills/docker-workflow/SKILL.md
```

### Delete a skill

```bash
rm -rf ~/.nova/skills/my-skill
```

Or ask Nova: *"Delete the my-skill skill."*

---

## Starter Skills

Nova ships with 7 starter skills. Copy them to your Nova home:

```bash
cp -r config/skills/* ~/.nova/skills/
```

| Skill | Category | Status | What it covers |
|-------|----------|--------|----------------|
| `python-coding` | development | ✅ Active | Type hints, PEP 8, pytest, ruff, mypy, venvs |
| `git-workflow` | development | ✅ Active | Branching, committing, pushing, PRs |
| `file-editing` | development | ✅ Active | Safe file editing patterns, verification steps |
| `code-review` | development | ✅ Active | Code review conventions and checklists |
| `documentation-template-builder` | development | ✅ Active | Generate docs in ai-companions style |
| `nova-development` | development | ✅ Active | Tool system, permissions, hooks, delegation, testing patterns, config, CI |
| `nova-debugging` | development | ✅ Active | Loops, hallucinations, context drift, tool failures, permission issues |

---

## Token Budget Considerations

Skills consume tokens in two places:

1. **Skills index** (always) — compact XML block in the system prompt. Each skill contributes ~1 line. Budget: `budgets.skills_max_chars` (default 15,000 chars).

2. **Loaded skill content** (on demand) — injected as a tool result when `skill_view` is called. Budget: `budgets.tool_result_max_chars` (default 8,000 chars).

### Tips for large skill libraries

- Keep descriptions short (under 100 chars) — they appear in the always-on index
- Split large skills into focused sub-skills rather than one giant file
- Adjust the budget if you have many skills:
  ```yaml
  budgets:
    skills_max_chars: 25000   # Allow more skills in the index
    skills_max_count: 100     # Allow more skills total
  ```

---

## Checklist

Before using a new skill:

- [ ] Directory is in `~/.nova/skills/<skill-name>/`
- [ ] File is named exactly `SKILL.md`
- [ ] Frontmatter has `name`, `category`, and `description`
- [ ] Description is specific and searchable (under 100 chars)
- [ ] Body is written as directives ("do X", "use Y"), not descriptions
- [ ] Common commands are complete and copy-pasteable
- [ ] Pitfalls section covers known gotchas
- [ ] Skill loads correctly: ask Nova *"Load the <name> skill"*

---

## Related Documentation

| Document | Purpose |
|----------|---------|
| [Creating Tools](GUIDE-001-CREATING_TOOLS.md) | Add new capabilities via Python instead of markdown |
| [Customizing Nova](GUIDE-003-CUSTOMIZING.md) | Skills config, budgets, and discovery |
| [Documentation Index](DOCUMENTATION_INDEX.md) | Full list of starter skills and their status |
| [CONTRIBUTING](../CONTRIBUTING.md) | PR workflow for contributing new skills |
