"""Declarative base class for SQLAlchemy 2.x and GeoAlchemy2 models."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base model class with metadata for all cadastral database models."""

    pass
