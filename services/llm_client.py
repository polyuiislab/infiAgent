#!/usr/bin/env python3
from utils.windows_compat import safe_print
# -*- coding: utf-8 -*-
"""
简化的LLM客户端 - 使用LiteLLM统一接口
"""

import os
import yaml
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from pathlib import Path
from litellm import completion  # 直接导入completion函数
import litellm


@dataclass
class ChatMessage:
    """聊天消息"""
    role: str
    content: str


@dataclass
class ToolCall:
    """工具调用"""
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class LLMResponse:
    """LLM响应"""
    status: str  # "success" or "error"
    output: str
    tool_calls: List[ToolCall]
    model: str
    finish_reason: str
    usage: Optional[Dict] = None
    error_information: str = ""


class SimpleLLMClient:
    """简化的LLM客户端 - 基于LiteLLM"""
    
    def __init__(self, llm_config_path: str = None, tools_config_path: str = None):
        """
        初始化LLM客户端
        
        Args:
            llm_config_path: LLM配置文件路径
            tools_config_path: 工具配置文件路径
        """
        # 加载LLM配置
        if llm_config_path is None:
            project_root = Path(__file__).parent.parent
            llm_config_path = project_root / "config" / "run_env_config" / "llm_config.yaml"
        
        if not os.path.exists(llm_config_path):
            raise FileNotFoundError(f"LLM配置文件不存在: {llm_config_path}")
        
        with open(llm_config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)
        
        # 读取配置
        self.base_url = self.config.get("base_url", "")
        self.api_key = self.config.get("api_key", "")
        self.temperature = self.config.get("temperature", 0)
        self.max_tokens = self.config.get("max_tokens", 0)
        self.max_context_window = self.config.get("max_context_window", 100000)  # 上下文窗口限制
        
        # 解析模型配置（支持两种格式）
        self.models = []  # 模型名称列表
        self.figure_models = []
        self.compressor_models = []
        self.model_configs = {}  # 模型名称 -> 配置字典
        
        self._parse_models_config(self.config.get("models", []), self.models)
        self._parse_models_config(self.config.get("figure_models", []), self.figure_models)
        self._parse_models_config(self.config.get("compressor_models", []), self.compressor_models)

        
        if not self.api_key:
            raise ValueError("未配置API密钥")
        
        if not self.models:
            raise ValueError("未配置可用模型列表")
        
        # 加载工具配置
        self.tools_config = {}
        if tools_config_path and os.path.exists(tools_config_path):
            with open(tools_config_path, 'r', encoding='utf-8') as f:
                self.tools_config = yaml.safe_load(f)
        
        # 配置LiteLLM
        litellm.set_verbose = False  # 关闭详细日志
        litellm.drop_params = True  # 自动丢弃不支持的参数（如Anthropic不支持parallel_tool_calls）
        
        safe_print(f"✅ LLM客户端初始化成功（LiteLLM）")
        safe_print(f"   Base URL: {self.base_url}")
        safe_print(f"   可用模型: {len(self.models)} 个")
        safe_print(f"   Figure模型: {len(self.figure_models)} 个")
        safe_print(f"   Compressor模型: {len(self.compressor_models)} 个")
        safe_print(f"   默认Temperature: {self.temperature}")
        safe_print(f"   默认Max Tokens: {self.max_tokens}")
    
    def _parse_models_config(self, models_config: List, target_list: List):
        """
        解析模型配置，支持两种格式：
        1. 字符串格式：直接是模型名称
        2. 对象格式：包含 name 和额外参数
        
        Args:
            models_config: 原始模型配置列表
            target_list: 目标列表（self.models, self.figure_models 等）
        """
        for model_item in models_config:
            if isinstance(model_item, str):
                # 简单格式：直接是模型名称
                target_list.append(model_item)
                self.model_configs[model_item] = {}
            elif isinstance(model_item, dict):
                # 对象格式：包含额外参数
                model_name = model_item.get("name")
                if not model_name:
                    safe_print(f"⚠️ 模型配置缺少 'name' 字段，跳过: {model_item}")
                    continue
                
                target_list.append(model_name)
                # 保存除 name 外的所有参数
                extra_params = {k: v for k, v in model_item.items() if k != "name"}
                self.model_configs[model_name] = extra_params
                
                if extra_params:
                    safe_print(f"   📝 模型 {model_name} 配置了额外参数: {list(extra_params.keys())}")
            else:
                safe_print(f"⚠️ 不支持的模型配置格式，跳过: {model_item}")
    
    def chat(
        self,
        history: List[ChatMessage],
        model: str,
        system_prompt: str,
        tool_list: List[str],
        tool_choice: str = "required",
        temperature: float = None,
        max_tokens: int = None
    ) -> LLMResponse:
        """
        调用LLM进行对话
        
        Args:
            history: 对话历史
            model: 模型名称
            system_prompt: 系统提示词
            tool_list: 可用工具列表
            tool_choice: 工具选择策略
            temperature: 温度参数（None则使用配置文件默认值）
            max_tokens: 最大token数（None则使用配置文件默认值）
            
        Returns:
            LLMResponse对象
        """
        # 使用配置文件的默认值
        if temperature is None:
            temperature = self.temperature
        if max_tokens is None:
            max_tokens = self.max_tokens
        
        try:
            # 构建工具定义（OpenAI格式）
            tools_definition = self._build_tools_definition(tool_list)
            
            # 转换消息格式
            messages = [{"role": "system", "content": system_prompt}]
            messages.extend([{"role": msg.role, "content": msg.content} for msg in history])
            
            # 构建请求参数
            kwargs = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "api_key": self.api_key,
            }
            
            # 只在 base_url 非空时添加 api_base（对于 Google/Anthropic 等官方 API，留空让 litellm 自动路由）
            if self.base_url:
                kwargs["api_base"] = self.base_url
            
            # 只在max_tokens > 0时添加
            if max_tokens > 0:
                kwargs["max_tokens"] = max_tokens
            
            # 添加工具定义
            if tools_definition:
                kwargs["tools"] = tools_definition
                if tool_choice == "required":
                    # litellm 会自动将 tool_choice 转换为各模型的格式
                    # OpenAI: tool_choice="required"
                    # Gemini: tool_config={function_calling_config: {mode: "ANY"}}
                    kwargs["tool_choice"] = "required"
                # 禁用并行工具调用（每次只调用一个工具）
                # 注意：Gemini 不支持 parallel_tool_calls，但 litellm.drop_params=True 会自动丢弃
                kwargs["parallel_tool_calls"] = False
            
            # 添加模型特定的额外参数
            model_extra_params = self.model_configs.get(model, {})
            if model_extra_params:
                # 处理 provider 参数（OpenRouter 特定）
                if "provider" in model_extra_params:
                    if "extra_body" not in kwargs:
                        kwargs["extra_body"] = {}
                    kwargs["extra_body"]["provider"] = model_extra_params["provider"]
                
                # 处理 extra_headers
                if "extra_headers" in model_extra_params:
                    kwargs["extra_headers"] = model_extra_params["extra_headers"]
                
                # 处理 extra_body（合并到已有的 extra_body）
                if "extra_body" in model_extra_params:
                    if "extra_body" not in kwargs:
                        kwargs["extra_body"] = {}
                    kwargs["extra_body"].update(model_extra_params["extra_body"])
                
                safe_print(f"   ⚙️  应用模型额外参数: {list(model_extra_params.keys())}")
            
            # 使用LiteLLM调用
            # 添加调试信息
            safe_print(f"   📝 System Prompt长度: {len(system_prompt)} 字符")
            safe_print(f"   🔧 工具数量: {len(tools_definition)}")
            safe_print(f"   📨 消息数量: {len(messages)}")
            
            response = completion(**kwargs)  # 使用导入的函数
            
            # 解析响应（参考原项目的安全解析方式）
            if response.choices and len(response.choices) > 0:
                choice = response.choices[0]
                message = choice.message
                
                output_text = message.content or ""
                tool_calls = []
                
                # 安全解析工具调用
                if hasattr(message, 'tool_calls') and message.tool_calls:
                    for tc in message.tool_calls:
                        import json
                        # 安全解析参数
                        try:
                            if isinstance(tc.function.arguments, str):
                                arguments = json.loads(tc.function.arguments)
                            else:
                                arguments = tc.function.arguments
                        except:
                            arguments = {}
                        
                        tool_calls.append(ToolCall(
                            id=tc.id,
                            name=tc.function.name,
                            arguments=arguments
                        ))
                
                # 安全提取usage信息
                usage = None
                if hasattr(response, 'usage') and response.usage:
                    usage = {
                        "prompt_tokens": getattr(response.usage, 'prompt_tokens', 0),
                        "completion_tokens": getattr(response.usage, 'completion_tokens', 0),
                        "total_tokens": getattr(response.usage, 'total_tokens', 0)
                    }
            else:
                return LLMResponse(
                    status="error",
                    output="",
                    tool_calls=[],
                    model=model,
                    finish_reason="error",
                    error_information="响应格式异常：缺少choices字段"
                )
            
            return LLMResponse(
                status="success",
                output=output_text,
                tool_calls=tool_calls,
                model=response.model,
                finish_reason=response.choices[0].finish_reason,
                usage=usage
            )
        
        except Exception as e:
            import traceback
            error_detail = traceback.format_exc()
            return LLMResponse(
                status="error",
                output="",
                tool_calls=[],
                model=model,
                finish_reason="error",
                error_information=f"{str(e)}\n\nDetails:\n{error_detail}"
            )
    
    def set_tools_config(self, tools_config: Dict):
        """
        设置工具配置（从ConfigLoader传入）
        
        Args:
            tools_config: 工具配置字典
        """
        self.tools_config = tools_config
    
    def _build_tools_definition(self, tool_list: List[str]) -> List[Dict]:
        """构建工具定义（OpenAI格式）"""
        if not self.tools_config:
            return []
        
        tools = []
        for tool_name in tool_list:
            if tool_name in self.tools_config:
                tool_config = self.tools_config[tool_name]
                tools.append({
                    "type": "function",
                    "function": {
                        "name": tool_config.get("name", tool_name),
                        "description": tool_config.get("description", ""),
                        "parameters": tool_config.get("parameters", {})
                    }
                })
        
        return tools


if __name__ == "__main__":
    # 测试LLM客户端
    try:
        client = SimpleLLMClient()
        safe_print(f"✅ 可用模型: {client.models}")
        
        # 测试简单调用
        history = [ChatMessage(role="user", content="请输出下一个动作")]
        response = client.chat(
            history=history,
            model=client.models[0],  # 使用第一个可用模型
            system_prompt="你是一个AI助手，请使用工具来完成任务。",
            tool_list=["file_read", "file_write"],
            tool_choice="required"
        )
        
        safe_print(f"✅ 响应状态: {response.status}")
        safe_print(f"✅ 工具调用数量: {len(response.tool_calls)}")
        if response.tool_calls:
            safe_print(f"✅ 第一个工具: {response.tool_calls[0].name}")
    except Exception as e:
        safe_print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
