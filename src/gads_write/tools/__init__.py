"""MCP tool functions, one module per group.

Tool functions are deliberately thin. Each one validates nothing itself,
decides nothing itself, and mutates nothing itself:

    guard  ->  safety/guards.py     decides
    apply  ->  ads/executor.py      mutates

A tool that grew its own logic would be a tool that could grow its own
bypass, so anything resembling judgement belongs in the gate.

Phase 2 contains only the registry. The tools themselves arrive in Phase 3
(reads) and Phase 4 (the first mutation).
"""
