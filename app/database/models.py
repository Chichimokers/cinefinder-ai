"""Modelos ORM de CineFinder AI (17 tablas)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Movie(TimestampMixin, Base):
    __tablename__ = "movies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    original_title: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    genres: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    synopsis: Mapped[str | None] = mapped_column(Text, nullable=True)
    poster_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    backdrop_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    runtime: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    verification_status: Mapped[str] = mapped_column(
        String(24), default="unverified", nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)

    aliases: Mapped[list["MovieAlias"]] = relationship(back_populates="movie", cascade="all, delete-orphan")
    images: Mapped[list["MovieImage"]] = relationship(back_populates="movie", cascade="all, delete-orphan")
    sources: Mapped[list["MovieSource"]] = relationship(back_populates="movie", cascade="all, delete-orphan")
    candidates: Mapped[list["MovieCandidate"]] = relationship(back_populates="movie")
    publications: Mapped[list["TelegramCatalogPublication"]] = relationship(back_populates="movie")
    media_assets: Mapped[list["MediaAsset"]] = relationship(back_populates="movie")
    delivery_jobs: Mapped[list["DeliveryJob"]] = relationship(back_populates="movie")


class MovieAlias(Base):
    __tablename__ = "movie_aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    movie_id: Mapped[int] = mapped_column(ForeignKey("movies.id", ondelete="CASCADE"), nullable=False)
    alias: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    movie: Mapped["Movie"] = relationship(back_populates="aliases")


class MovieImage(Base):
    __tablename__ = "movie_images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    movie_id: Mapped[int] = mapped_column(ForeignKey("movies.id", ondelete="CASCADE"), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    movie: Mapped["Movie"] = relationship(back_populates="images")


class MovieSource(Base):
    __tablename__ = "movie_sources"
    __table_args__ = (
        UniqueConstraint("movie_id", "provider", "external_id", name="uq_movie_source_provider_external"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    movie_id: Mapped[int] = mapped_column(ForeignKey("movies.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    movie: Mapped["Movie"] = relationship(back_populates="sources")


class SearchJob(TimestampMixin, Base):
    __tablename__ = "search_jobs"
    __table_args__ = (Index("ix_search_jobs_status_created", "status", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    status: Mapped[str] = mapped_column(
        String(24), default="queued", nullable=False, index=True
    )  # queued|running|completed|failed|cancelled
    origin: Mapped[str] = mapped_column(String(24), default="api", nullable=False)  # api|telegram
    requester_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    query: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_hashes: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    image_urls: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    encrypted_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_movie_id: Mapped[int | None] = mapped_column(ForeignKey("movies.id"), nullable=True)
    result_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    result_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    steps_done: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    candidates: Mapped[list["MovieCandidate"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    tasks: Mapped[list["AgentTask"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    usage: Mapped[list["AgentUsage"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    history: Mapped[list["SearchHistory"]] = relationship(back_populates="job")


class MovieCandidate(Base):
    __tablename__ = "movie_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("search_jobs.id", ondelete="CASCADE"), nullable=False)
    movie_id: Mapped[int | None] = mapped_column(ForeignKey("movies.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    selected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    job: Mapped["SearchJob"] = relationship(back_populates="candidates")
    movie: Mapped["Movie | None"] = relationship(back_populates="candidates")


class SearchHistory(Base):
    __tablename__ = "search_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("search_jobs.id"), nullable=True)
    requester_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    query: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_hashes: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    job: Mapped["SearchJob | None"] = relationship(back_populates="history")


class AgentTask(Base):
    __tablename__ = "agent_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("search_jobs.id", ondelete="CASCADE"), nullable=False)
    step: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    job: Mapped["SearchJob"] = relationship(back_populates="tasks")


class AgentUsage(Base):
    __tablename__ = "agent_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("search_jobs.id", ondelete="CASCADE"), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    job: Mapped["SearchJob"] = relationship(back_populates="usage")


class TelegramChannel(Base):
    __tablename__ = "telegram_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    username: Mapped[str | None] = mapped_column(String(256), nullable=True)
    channel_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_private: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    purpose: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    publications: Mapped[list["TelegramCatalogPublication"]] = relationship(back_populates="channel")


class TelegramMessage(Base):
    __tablename__ = "telegram_messages"
    __table_args__ = (
        UniqueConstraint("chat_id", "message_id", name="uq_telegram_message_chat_msg"),
        Index("ix_telegram_messages_grouped", "grouped_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sender_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_hashes: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    grouped_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TelegramCatalogPublication(Base):
    __tablename__ = "telegram_catalog_publications"
    __table_args__ = (
        UniqueConstraint("movie_id", "channel_id", name="uq_catalog_publication_movie_channel"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    movie_id: Mapped[int] = mapped_column(ForeignKey("movies.id", ondelete="CASCADE"), nullable=False)
    channel_id: Mapped[int] = mapped_column(ForeignKey("telegram_channels.id"), nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    movie: Mapped["Movie"] = relationship(back_populates="publications")
    channel: Mapped["TelegramChannel"] = relationship(back_populates="publications")


class AuthorizedSource(Base):
    __tablename__ = "authorized_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    license: Mapped[str | None] = mapped_column(String(256), nullable=True)
    authorized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    media_assets: Mapped[list["MediaAsset"]] = relationship(back_populates="source")


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    movie_id: Mapped[int] = mapped_column(ForeignKey("movies.id", ondelete="CASCADE"), nullable=False)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("authorized_sources.id"), nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    license: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="registered", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    movie: Mapped["Movie"] = relationship(back_populates="media_assets")
    source: Mapped["AuthorizedSource | None"] = relationship(back_populates="media_assets")
    delivery_jobs: Mapped[list["DeliveryJob"]] = relationship(back_populates="asset")


class DeliveryJob(Base):
    __tablename__ = "delivery_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    movie_id: Mapped[int] = mapped_column(ForeignKey("movies.id", ondelete="CASCADE"), nullable=False)
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("media_assets.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False, index=True)
    requester_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    movie: Mapped["Movie"] = relationship(back_populates="delivery_jobs")
    asset: Mapped["MediaAsset | None"] = relationship(back_populates="delivery_jobs")


class AppUser(Base):
    __tablename__ = "app_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(256), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_authorized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
