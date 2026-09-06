from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Experience(BaseModel):
    organization: str = ""
    role: str = ""
    start_date: str = ""
    end_date: str = ""
    description: str = ""


class Project(BaseModel):
    name: str = ""
    role: str = ""
    start_date: str = ""
    end_date: str = ""
    description: str = ""
    technologies: list[str] = Field(default_factory=list)


class Education(BaseModel):
    school: str = ""
    degree: str = ""
    major: str = ""
    start_date: str = ""
    end_date: str = ""


class ResumeProfile(BaseModel):
    name: str = ""
    gender: Literal["男", "女", "其他", "未识别"] = "未识别"
    age: int | None = None
    phone: str = ""
    email: str = ""
    location: str = ""
    target_role: str = ""
    summary: str = ""
    education: list[Education] = Field(default_factory=list)
    internships: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)


class ResumeRecord(BaseModel):
    id: str
    filename: str
    label: str
    profile: ResumeProfile
    parser: str
    created_at: datetime
    updated_at: datetime


class ResumeUpdate(BaseModel):
    label: str | None = None
    profile: ResumeProfile | None = None
