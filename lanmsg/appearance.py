"""Validated server-wide design tokens, mapped to CSS variables by the client."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Color = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]


class Appearance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    background: Color = "#101419"
    sidebar: Color = "#171d24"
    panel: Color = "#1c242e"
    text: Color = "#e2e8f0"
    muted: Color = "#94a3b8"
    accent: Color = "#72d6bb"
    border: Color = "#303b48"
    nickname: Color = "#91b9f5"
    font: Literal["mono", "sans"] = "mono"
    font_size: int = Field(default=14, ge=12, le=20)
    line_height: float = Field(default=1.6, ge=1.3, le=2.2)
    row_spacing: int = Field(default=3, ge=0, le=10)
    corner_radius: int = Field(default=6, ge=0, le=16)
