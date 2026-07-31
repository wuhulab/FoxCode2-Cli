"""Skills 管理：可复用知识/工作流，按需加载进上下文。"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic_ai import RunContext

from .models import WorkspaceDeps
from .tools import permission_validator


@dataclass
class Skill:
    name: str
    description: str
    content: str
    path: Path


@dataclass
class SkillsManager:
    skills_dir: Path
    skills: dict[str, Skill] = field(default_factory=dict)

    def load(self):
        self.skills = {}
        skills_dir = self.skills_dir
        if not skills_dir.is_dir():
            return
        for entry in sorted(skills_dir.iterdir()):
            if entry.is_dir():
                skill_file = entry / "SKILL.md"
                if not skill_file.is_file():
                    continue
                name = entry.name
            elif entry.suffix.lower() in (".md", ".txt"):
                skill_file = entry
                name = entry.stem
            else:
                continue
            try:
                text = skill_file.read_text(encoding="utf-8")
            except Exception:
                continue
            front, body = _split_frontmatter(text)
            meta = {}
            if front:
                try:
                    meta = yaml.safe_load(front) or {}
                except Exception:
                    meta = {}
            name = str(meta.get("name") or name).strip().lower()
            desc = str(meta.get("description") or "").strip()
            if not desc:
                first_lines = [l for l in body.strip().splitlines() if l.strip()]
                desc = first_lines[0][:120] if first_lines else name
            self.skills[name] = Skill(
                name=name,
                description=desc,
                content=body.strip(),
                path=skill_file,
            )

    def list(self) -> list[Skill]:
        return sorted(self.skills.values(), key=lambda s: s.name)

    def get(self, name: str) -> Skill | None:
        key = name.strip().lower()
        return self.skills.get(key)


_FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _split_frontmatter(text: str) -> tuple[str | None, str]:
    m = _FRONT_RE.match(text)
    if m:
        return m.group(1), text[m.end() :]
    return None, text


def register(agent):
    @agent.tool(args_validator=permission_validator("use_skill"))
    async def use_skill(ctx: RunContext[WorkspaceDeps], name: str) -> str:
        ctx.deps.tool_tracker.count("use_skill")
        skills = ctx.deps.skills
        if skills is None:
            return "错误: 未加载 skills 管理器"
        skill = skills.get(name)
        if skill is None:
            available = ", ".join(s.name for s in skills.list()) or "(无)"
            return f"错误: 未找到 skill '{name}'，可用: {available}"
        return f"## Skill: {skill.name}\n\n{skill.content}"

    @agent.tool(args_validator=permission_validator("list_skills"))
    async def list_skills(ctx: RunContext[WorkspaceDeps]) -> str:
        ctx.deps.tool_tracker.count("list_skills")
        skills = ctx.deps.skills
        if skills is None or not skills.list():
            return "暂无可用 skills"
        lines = ["可用 skills:"]
        for s in skills.list():
            lines.append(f"  /{s.name} - {s.description}")
        return "\n".join(lines)
