import os
import re
from abc import ABC, abstractmethod

from agents import Agent, Runner

from .model_provider import configured_model
from .models import Education, Experience, Project, ResumeProfile


SECTION_NAMES = {
    "education": ("教育经历", "教育背景", "education"),
    "internships": ("实习经历", "工作经历", "工作经验", "experience", "internship"),
    "projects": ("项目经历", "项目经验", "projects", "project experience"),
    "skills": ("专业技能", "技能", "skills", "technical skills"),
}


class ResumeExtractor(ABC):
    name: str

    @abstractmethod
    async def parse(self, text: str) -> ResumeProfile: ...


def _section(text: str, wanted: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    start = None
    for i, line in enumerate(lines):
        normalized = line.lower().strip("：: ")
        if any(title in normalized for title in SECTION_NAMES[wanted]):
            start = i + 1
            break
    if start is None:
        return ""
    output = []
    all_titles = [t for titles in SECTION_NAMES.values() for t in titles]
    for line in lines[start:]:
        normalized = line.lower().strip("：: ")
        if any(normalized == title for title in all_titles):
            break
        output.append(line)
    return "\n".join(output)


class RuleBasedExtractor(ResumeExtractor):
    name = "local-rules"

    async def parse(self, text: str) -> ResumeProfile:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        email = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text)
        phone = re.search(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)", text)
        age = re.search(r"(?:年龄|Age)\s*[：:]?\s*(\d{2})", text, re.I)
        gender = re.search(r"(?:性别|Gender)\s*[：:]?\s*(男|女|其他|male|female)", text, re.I)
        gender_value = "未识别"
        if gender:
            gender_value = {"male": "男", "female": "女"}.get(gender.group(1).lower(), gender.group(1))

        name = ""
        named = re.search(r"(?:姓名|Name)\s*[：:]\s*([^\s|]{2,30})", text, re.I)
        if named:
            name = named.group(1)
        elif lines and len(lines[0]) <= 24 and not re.search(r"@|\d{5,}", lines[0]):
            name = lines[0]

        skills_text = _section(text, "skills")
        skills = [s.strip(" ·•-") for s in re.split(r"[,，、|/\n]", skills_text) if s.strip(" ·•-")][:24]

        internship_text = _section(text, "internships")
        project_text = _section(text, "projects")
        education_text = _section(text, "education")
        internships = [Experience(description=internship_text)] if internship_text else []
        projects = [Project(name="待确认项目", description=project_text)] if project_text else []
        education = [Education(school=education_text)] if education_text else []

        return ResumeProfile(
            name=name,
            gender=gender_value,
            age=int(age.group(1)) if age else None,
            phone=phone.group(0) if phone else "",
            email=email.group(0) if email else "",
            education=education,
            internships=internships,
            projects=projects,
            skills=skills,
        )


class AgentsSDKExtractor(ResumeExtractor):
    name = "openai-agents-sdk"

    async def parse(self, text: str) -> ResumeProfile:
        model, settings = configured_model()
        agent = Agent(
            name="Zhida Resume Parser",
            model=model,
            model_settings=settings,
            output_type=ResumeProfile,
            instructions=(
                "你是严谨的中英文求职简历解析 Agent。将每段教育、实习和项目分别拆成独立记录。"
                "只提取原文明确出现的信息，不猜测性别、年龄、日期、成果或敏感信息。"
                "保留量化成果和技术栈；无法识别时使用空值，不得编造。"
            ),
        )
        result = await Runner.run(agent, "请结构化以下简历：\n\n" + text[:50000], max_turns=3)
        if not isinstance(result.final_output, ResumeProfile):
            raise RuntimeError("模型没有返回有效的简历结构")
        return result.final_output


def get_resume_extractor() -> ResumeExtractor:
    if os.getenv("APP_AGENT_MODE", "agents_sdk").lower() == "rules":
        return RuleBasedExtractor()
    return AgentsSDKExtractor()
