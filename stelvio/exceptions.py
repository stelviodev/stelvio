class StelvioProjectError(Exception):
    """Raised when no Stelvio project is found in the current or parent directories."""


class AppRenamedError(Exception):
    """Raised when the app name has changed since last deployment."""

    def __init__(self, old_name: str, new_name: str):
        self.old_name = old_name
        self.new_name = new_name
        super().__init__(f"App renamed from '{old_name}' to '{new_name}'")
