"""Resolution learning loop — the rule-release eval battery that gates a
proposed alias before it can change live behavior. The system learns in
proposal-space; nothing reaches the resolver until it passes this gate.
See docs/RESOLUTION_LEARNING_LOOP.md."""
