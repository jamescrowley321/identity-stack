"""Repository layer — data access only, no business logic."""

from app.repositories.base import BaseRepository, RepositoryConflictError

__all__ = ["BaseRepository", "RepositoryConflictError"]
