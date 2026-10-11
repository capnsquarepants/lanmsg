"""Owner-editable application settings; operational limits apply at startup."""

import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server_name: str = Field(default="LANmsg", min_length=1, max_length=64)
    server_description: str = Field(default="Your private network", max_length=160)
    registration_enabled: bool = True
    media_previews: bool = False
    connections: int = Field(default=100, ge=1, le=1000)
    connections_per_ip: int = Field(default=20, ge=1, le=1000)
    connections_per_session: int = Field(default=3, ge=1, le=10)
    http_concurrency: int = Field(default=32, ge=1, le=128)
    message_rate: float = Field(default=2, ge=.001, le=20)
    message_burst: int = Field(default=10, ge=1, le=100)

    @field_validator("server_name", "server_description")
    @classmethod
    def clean_text(cls, value, info):
        value = value.strip()
        if (info.field_name == "server_name" and not value) or any(unicodedata.category(char).startswith("C") for char in value):
            raise ValueError("Use visible text without control characters")
        return value

    def public(self):
        return self.model_dump(include={"server_name", "server_description", "registration_enabled", "media_previews"})


LIMIT_FIELDS = ("connections", "connections_per_ip", "connections_per_session", "http_concurrency", "message_rate", "message_burst")
