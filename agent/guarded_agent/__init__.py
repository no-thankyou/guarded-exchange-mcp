"""Console agent client of guarded-exchange-mcp (FR-39, ADR-016).

It reaches the server only over MCP (stdio, HTTP or in-process) and asks an LLM provider through
the ``LLMProvider`` port, so OpenAI and Anthropic are interchangeable adapters.
"""
