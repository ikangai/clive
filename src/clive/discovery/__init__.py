"""Self-learning tool discovery (gh#41).

Manual entry point: ``clive --explore <tool>``. The pipeline runs
``explore_tool`` + ``generate_driver`` + ``write_generated_driver``.

Submodules:
    models  -- ExplorationResult / ProbeOutcome dataclasses
    prompts -- exploration goal builder, generation prompt builder, safety lists
    explorer -- explore_tool() adapter over run_subtask_interactive
    generator -- generate_driver() LLM synthesis + write_generated_driver()
    refiner  -- refine_driver() eval-failure-driven re-synthesis (Phase 3)
"""
