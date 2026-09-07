"""Domain exceptions raised by QLog MCP operations."""


class QLogMcpError(RuntimeError):
    """Base class for expected QLog MCP failures."""


class DatabaseNotConfiguredError(QLogMcpError):
    """Raised when an operation needs a database but none was discovered."""


class DatabaseNotFoundError(QLogMcpError):
    """Raised when the configured database path does not exist."""


class InvalidQueryError(QLogMcpError):
    """Raised when a semantic query is invalid."""


class IncompatibleDatabaseError(QLogMcpError):
    """Raised when the selected database does not provide required QLog data."""
