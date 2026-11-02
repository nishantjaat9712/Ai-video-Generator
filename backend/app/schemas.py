from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .models import uid

class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resolution: Literal["480p", "720p"] = "720p"
    aspect_ratio: Literal["9:16", "16:9", "1:1"] = "9:16"
    duration: int = Field(default=45, ge=45, le=120, multiple_of=5)
    fps: Literal[16, 24, 25, 30] = 16
    style: Literal["Realistic", "Cinematic", "Anime", "3D", "Fantasy"] = "Cinematic"
    camera: Literal["Static", "Pan", "Zoom", "Tracking", "Handheld"] = "Tracking"
    quality: Literal["Fast", "Balanced", "High"] = "Balanced"
    seed: int | None = Field(default=None, ge=0, le=2147483647)
    image_asset_id: str | None = None
    reference_asset_id: str | None = None
    narration_enabled: bool = False
    voice: str = Field(default="alloy", max_length=100)
    subtitles: bool = True
    character_ids: list[str] = Field(default_factory=list, max_length=10)
    music_asset_id: str | None = None
    audio_volume: float = Field(default=1, ge=0, le=1)
    music_volume: float = Field(default=0.15, ge=0, le=1)
    noise_reduction: bool = False

class Scene(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=uid, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    prompt: str = Field(min_length=1, max_length=8000)
    narration: str = Field(default="", max_length=4000)
    duration: Literal[5, 10] = 5
    trim_start: float | None = Field(default=None, ge=0, le=60)
    trim_end: float | None = Field(default=None, gt=0, le=60)
    asset_id: str | None = None
    status: str = "draft"
    voice: str = Field(default='', max_length=100)
    camera: str = Field(default='', max_length=100)
    sound_effect: str = Field(default='', max_length=300)
    @model_validator(mode="after")
    def valid_trim(self):
        if self.trim_end is not None and self.trim_end <= (self.trim_start or 0):
            raise ValueError("trim_end must be after trim_start")
        return self

class ProjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="Untitled story", min_length=1, max_length=160)
    prompt: str = Field(default="", max_length=30000)
    mode: Literal["text", "image", "script"] = "text"
    language: Literal["Hindi", "English", "Hinglish"] = "Hindi"
    provider: Literal["fal", "comfyui"] = "fal"
    settings: Settings = Field(default_factory=Settings)
    scenes: list[Scene] = Field(default_factory=list, max_length=30)
    output_asset_id: str | None = None
    metadata: dict = Field(default_factory=dict)
    @model_validator(mode="after")
    def unique_scene_ids(self):
        ids = [s.id for s in self.scenes]
        if len(ids) != len(set(ids)):
            raise ValueError("Scene IDs must be unique")
        return self

class CharacterInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    age_range: str = Field(default="Adult", max_length=100)
    appearance: str = Field(default="", max_length=3000)
    clothing: str = Field(default="", max_length=2000)
    personality: str = Field(default="", max_length=2000)
    voice: str = Field(default="", max_length=100)
    reference_asset_id: str | None = None

class GenerateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_id: str | None = None

class RetryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    acknowledge_duplicate_risk: bool = False

