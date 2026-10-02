class ToolError(Exception):
    """A tool can't do what it was asked. The message goes back to the Planner as-is."""
