"""Skills 管理：可复用知识/工作流，按需加载进上下文。"""

from __future__ import annotations

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
    base_dir: Path | None = None


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
                base_dir = entry
            elif entry.suffix.lower() in (".md", ".txt"):
                skill_file = entry
                name = entry.stem
                base_dir = None
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
                base_dir=base_dir,
            )

    def list(self) -> list[Skill]:
        return sorted(self.skills.values(), key=lambda s: s.name)

    def get(self, name: str) -> Skill | None:
        key = name.strip().lower()
        return self.skills.get(key)

    def list_files(self, name: str) -> list[str] | None:
        """返回目录型 skill 下所有可读取的附属文件路径（相对于 base_dir）。"""
        skill = self.get(name)
        if skill is None or skill.base_dir is None:
            return None
        files: list[str] = []
        for p in sorted(skill.base_dir.rglob("*")):
            if p.is_file() and p.name != "SKILL.md":
                try:
                    rel = p.relative_to(skill.base_dir).as_posix()
                    files.append(rel)
                except ValueError:
                    continue
        return files


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
        result = f"## Skill: {skill.name}\n\n{skill.content}"
        if skill.base_dir is not None:
            result += (
                f"\n\n此 skill 包含附属文件。"
                f"如需查看可用文件，调用 list_skill_files('{skill.name}')；"
                f"如需读取具体文件，调用 use_skill_file('{skill.name}', '<file_path>')。"
            )
        return result

    @agent.tool(args_validator=permission_validator("list_skills"))
    async def list_skills(ctx: RunContext[WorkspaceDeps]) -> str:
        ctx.deps.tool_tracker.count("list_skills")
        skills = ctx.deps.skills
        if skills is None or not skills.list():
            return "暂无可用 skills"
        lines = ["可用 skills:"]
        for s in skills.list():
            tag = " [目录]" if s.base_dir else ""
            lines.append(f"  /{s.name}{tag} - {s.description}")
        return "\n".join(lines)

    @agent.tool(args_validator=permission_validator("list_skill_files"))
    async def list_skill_files(ctx: RunContext[WorkspaceDeps], name: str) -> str:
        ctx.deps.tool_tracker.count("list_skill_files")
        skills = ctx.deps.skills
        if skills is None:
            return "错误: 未加载 skills 管理器"
        skill = skills.get(name)
        if skill is None:
            available = ", ".join(s.name for s in skills.list()) or "(无)"
            return f"错误: 未找到 skill '{name}'，可用: {available}"
        files = skills.list_files(name)
        if files is None:
            return f"错误: Skill '{name}' 不是目录型 skill，没有附属文件。"
        if not files:
            return f"Skill '{name}' 目录下暂无附属文件。"
        lines = [f"Skill '{name}' 可用附属文件（用 use_skill_file 读取）:"]
        for f in files:
            lines.append(f"  {f}")
        return "\n".join(lines)

    @agent.tool(args_validator=permission_validator("use_skill_file"))
    async def use_skill_file(
        ctx: RunContext[WorkspaceDeps], name: str, file_path: str
    ) -> str:
        ctx.deps.tool_tracker.count("use_skill_file")
        skills = ctx.deps.skills
        if skills is None:
            return "错误: 未加载 skills 管理器"
        skill = skills.get(name)
        if skill is None:
            available = ", ".join(s.name for s in skills.list()) or "(无)"
            return f"错误: 未找到 skill '{name}'，可用: {available}"
        if skill.base_dir is None:
            return f"错误: Skill '{name}' 不是目录型 skill，没有附属文件。"
        target = (skill.base_dir / file_path).resolve()
        try:
            target.relative_to(skill.base_dir.resolve())
        except ValueError:
            return "错误: file_path 超出 skill 目录范围"
        if not target.is_file():
            return f"错误: 文件不存在: {file_path}"
        try:
            text = target.read_text(encoding="utf-8")
        except Exception as e:
            return f"错误: 无法读取文件: {e}"
        # Strip frontmatter for consistency
        _, body = _split_frontmatter(text)
        return f"## Skill File: {skill.name}/{file_path}\n\n{body.strip()}"
