#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Skill 加载器 - 遵循 Agent Skills 开放标准 (agentskills.io)

职责：
1. 扫描 ~/.mla_v3/skills_library/ 目录发现所有 skills
2. 解析 SKILL.md frontmatter（name + description）
3. 生成 <available_skills> XML 片段注入 system prompt
"""

import yaml
from pathlib import Path
from typing import List, Dict, Optional


class SkillLoader:
    """Skill 加载器"""
    
    def __init__(self, skills_library_path: str = None):
        """
        初始化
        
        Args:
            skills_library_path: skills 仓库路径，默认 ~/mla_v3/skills_library/（Docker 中为 /root/mla_v3/skills_library/）
        """
        if skills_library_path:
            self.skills_library = Path(skills_library_path)
        else:
            self.skills_library = Path.home() / "mla_v3" / "skills_library"  # 与 conversation_storage 同级
        
        # 确保目录存在
        self.skills_library.mkdir(parents=True, exist_ok=True)
        
        # 缓存已解析的 skill 元数据
        self._metadata_cache: Optional[List[Dict]] = None
    
    def discover_skills(self) -> List[Dict]:
        """
        扫描 skills_library 目录，解析所有 SKILL.md 的 frontmatter
        
        Returns:
            [{name, description, path, license?, compatibility?}, ...] 列表
        """
        if self._metadata_cache is not None:
            return self._metadata_cache
        
        skills = []
        
        if not self.skills_library.exists():
            self._metadata_cache = skills
            return skills
        
        # 扫描一级子目录
        for skill_dir in sorted(self.skills_library.iterdir()):
            if not skill_dir.is_dir():
                continue
            
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            
            metadata = self._parse_frontmatter(skill_md)
            if metadata:
                metadata["path"] = str(skill_dir)
                metadata["skill_md_path"] = str(skill_md)
                skills.append(metadata)
        
        self._metadata_cache = skills
        return skills
    
    def _parse_frontmatter(self, skill_md_path: Path) -> Optional[Dict]:
        """
        解析 SKILL.md 的 YAML frontmatter
        
        Args:
            skill_md_path: SKILL.md 文件路径
            
        Returns:
            {name, description, ...} 或 None（解析失败时）
        """
        try:
            content = skill_md_path.read_text(encoding='utf-8')
            
            # 提取 YAML frontmatter（--- 包裹）
            if not content.startswith('---'):
                return None
            
            # 找到第二个 ---
            end_idx = content.index('---', 3)
            frontmatter_str = content[3:end_idx].strip()
            
            frontmatter = yaml.safe_load(frontmatter_str)
            if not isinstance(frontmatter, dict):
                return None
            
            # 验证必需字段
            name = frontmatter.get("name")
            description = frontmatter.get("description")
            
            if not name or not description:
                return None
            
            result = {
                "name": name,
                "description": description
            }
            
            # 可选字段
            if frontmatter.get("license"):
                result["license"] = frontmatter["license"]
            if frontmatter.get("compatibility"):
                result["compatibility"] = frontmatter["compatibility"]
            
            return result
        
        except Exception:
            return None
    
    def build_available_skills_xml(self) -> str:
        """
        生成 <available_skills> XML 片段，用于注入 system prompt
        
        遵循官方推荐格式：每个 skill ~100 tokens
        
        Returns:
            XML 字符串，如果没有 skills 则返回空字符串
        """
        skills = self.discover_skills()
        
        if not skills:
            return ""
        
        xml_parts = ["<available_skills>"]
        for skill in skills:
            xml_parts.append(f'  <skill>')
            xml_parts.append(f'    <name>{skill["name"]}</name>')
            xml_parts.append(f'    <description>{skill["description"]}</description>')
            # location 用相对于 workspace 的 .skills/ 路径（部署后的位置）
            xml_parts.append(f'    <location>.skills/{skill["name"]}/SKILL.md</location>')
            xml_parts.append(f'  </skill>')
        xml_parts.append("</available_skills>")
        xml_parts.append("")
        xml_parts.append("提示：需要使用某个 skill 时，先调用 load_skill 工具将其部署到 workspace，然后使用 file_read 读取 SKILL.md 获取详细指令。")
        
        return "\n".join(xml_parts)
    
    def get_skill_source_path(self, skill_name: str) -> Optional[Path]:
        """
        获取指定 skill 在 skills_library 中的源路径
        
        Args:
            skill_name: skill 名称
            
        Returns:
            skill 目录的 Path，不存在则返回 None
        """
        skill_dir = self.skills_library / skill_name
        skill_md = skill_dir / "SKILL.md"
        
        if skill_dir.is_dir() and skill_md.exists():
            return skill_dir
        
        return None


# 全局实例缓存
_skill_loader_instance: Optional[SkillLoader] = None


def get_skill_loader(skills_library_path: str = None) -> SkillLoader:
    """获取全局 SkillLoader 实例"""
    global _skill_loader_instance
    if _skill_loader_instance is None:
        _skill_loader_instance = SkillLoader(skills_library_path)
    return _skill_loader_instance
