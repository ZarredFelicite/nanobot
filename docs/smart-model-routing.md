# Smart Model Routing

## Concept

Route simple messages to cheaper/faster models while keeping complex tasks on the primary model. The goal is cost savings without sacrificing quality on tasks that need it.

## Classification Criteria

A message is considered **simple** when it meets ALL of:
- Under 200 characters
- Under 30 words
- No code markers (triple backticks, indented blocks)
- No keywords suggesting complexity: "refactor", "implement", "debug", "analyze", "design", "architect", "migrate", "optimize"
- No file references (@file:, paths with `/`)
- Not a follow-up to a tool-using turn

A message is considered **complex** when ANY of:
- Over 500 characters or 80 words
- Contains code blocks or multiple file paths
- Contains complexity keywords
- References prior tool results
- Multi-part requests (numbered lists, "and then", "also")

Everything in between defaults to the primary model (conservative approach).

## Config Structure

```json
{
  "agents": {
    "defaults": {
      "model": "anthropic/claude-sonnet-4-5",
      "routingEnabled": false,
      "simpleModel": "openrouter/google/gemini-2.0-flash-lite-001",
      "complexModel": null
    }
  }
}
```

- `routingEnabled`: opt-in, off by default
- `simpleModel`: used for greetings, yes/no answers, short clarifications
- `complexModel`: if set, overrides `model` for complex tasks (allows a three-tier setup)
- When routing is disabled or classification is uncertain, always use `model`

## Integration Point

Before the LLM call in `_run_agent_loop()`:
1. Classify the latest user message
2. If simple and no active tool chain, swap model for this turn only
3. Log the routing decision for observability

## Future Enhancements

- Learn from user feedback (if user immediately re-asks, the simple model failed)
- Per-tool-chain routing (once tools are invoked, stay on primary model)
- Token cost tracking per model tier
- A/B testing framework for routing accuracy
