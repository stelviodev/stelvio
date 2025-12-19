class StelvioProjectError(Exception):
    """Raised when no Stelvio project is found in the current or parent directories."""


class StateLockedError(Exception):
    """Raised when trying to acquire a lock on state that's already locked."""

    def __init__(self, command: str, created: str, update_id: str, env: str):
        self.command = command
        self.created = created
        self.update_id = update_id
        self.env = env
        super().__init__(
            f"Environment locked by '{command}' since {created}. "
            f"Run 'stlv unlock {env}' to force unlock."
        )
