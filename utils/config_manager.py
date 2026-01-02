#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置管理工具
"""

import yaml
import json
from pathlib import Path


def get_config_path(config_name: str = "llm_config") -> Path:
    """获取配置文件路径（包内）"""
    # 找到包的位置
    module_path = Path(__file__).parent.parent
    config_file = module_path / "config" / "run_env_config" / f"{config_name}.yaml"
    return config_file


def show_config(config_name: str = "llm_config"):
    """显示配置"""
    config_file = get_config_path(config_name)
    
    if not config_file.exists():
        print(f"❌ 配置文件不存在: {config_file}")
        return
    
    with open(config_file, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    print(f"\n📋 配置文件: {config_file}")
    print(f"{'='*80}")
    print(yaml.dump(config, allow_unicode=True, default_flow_style=False))
    print(f"{'='*80}\n")


def set_config(key: str, value: str, config_name: str = "llm_config"):
    """
    设置配置项
    
    Args:
        key: 配置键，支持点号分隔（如 llm.api_key）
        value: 配置值
        config_name: 配置文件名
    """
    config_file = get_config_path(config_name)
    
    if not config_file.exists():
        print(f"❌ 配置文件不存在: {config_file}")
        return
    
    # 读取配置
    with open(config_file, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f) or {}
    
    # 解析键路径
    keys = key.split('.')
    current = config
    
    for k in keys[:-1]:
        if k not in current:
            current[k] = {}
        current = current[k]
    
    # 设置值（尝试智能转换类型）
    final_key = keys[-1]
    
    # 尝试转换类型
    if value.lower() in ['true', 'false']:
        current[final_key] = value.lower() == 'true'
    elif value.isdigit():
        current[final_key] = int(value)
    elif value.replace('.', '', 1).isdigit():
        current[final_key] = float(value)
    elif value.startswith('[') and value.endswith(']'):
        # 列表格式：尝试作为 JSON 解析
        try:
            # 首先尝试作为标准 JSON 数组解析
            current[final_key] = json.loads(value)
        except json.JSONDecodeError:
            # 如果失败，按简单逗号分割处理
            items = value[1:-1].split(',')
            current[final_key] = [item.strip().strip('"').strip("'") for item in items if item.strip()]
    else:
        current[final_key] = value
    
    # 写回配置
    with open(config_file, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    
    print(f"✅ 配置已更新: {key} = {current[final_key]}")
    print(f"   配置文件: {config_file}")


def reset_config(config_name: str = "llm_config"):
    """重置配置（显示路径，让用户手动编辑）"""
    config_file = get_config_path(config_name)
    print(f"\n📄 配置文件位置: {config_file}")
    print(f"💡 您可以直接编辑此文件，或使用:")
    print(f"   mla-agent --config-set KEY VALUE")
    print(f"\n常用配置:")
    print(f"   --config-set api_key \"YOUR_KEY\"")
    print(f"   --config-set base_url \"https://api.openai.com/v1\"")
    print(f"   --config-set models \"[gpt-4o,gpt-4o-mini]\"")
    print()


if __name__ == "__main__":
    # 测试
    print("查看配置:")
    show_config()
    
    print("\n设置 API key:")
    set_config("api_key", "test-key-123")
    
    print("\n再次查看:")
    show_config()

