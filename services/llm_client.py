#!/usr/bin/env python3
from utils.windows_compat import safe_print
# -*- coding: utf-8 -*-
"""
简化的LLM客户端 - 使用LiteLLM统一接口
"""

import os
import yaml
import time
import json
import concurrent.futures
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
        
        # 读取超时配置（默认值：600s, 20s, 20s）
        self.timeout = self.config.get("timeout", 600)  # LiteLLM 原生：总超时
        self.stream_timeout = self.config.get("stream_timeout", 20)  # LiteLLM 原生：流式超时
        self.first_chunk_timeout = self.config.get("first_chunk_timeout", 20)  # 应用层强制：首包超时
        
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
        safe_print(f"   超时配置: timeout={self.timeout}s, stream_timeout={self.stream_timeout}s, first_chunk_timeout={self.first_chunk_timeout}s")
    
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
        max_tokens: int = None,
        max_retries: int = 3
    ) -> LLMResponse:
        """
        调用LLM进行对话 (增强版：支持流式监控、自动重试、参数修复)
        
        Args:
            history: 对话历史
            model: 模型名称
            system_prompt: 系统提示词
            tool_list: 可用工具列表
            tool_choice: 工具选择策略
            temperature: 温度参数（None则使用配置文件默认值）
            max_tokens: 最大token数（None则使用配置文件默认值）
            max_retries: 最大重试次数（默认3次，即总共最多4次尝试）
            
        Returns:
            LLMResponse对象
        """
        # 使用配置文件的默认值
        if temperature is None:
            temperature = self.temperature
        if max_tokens is None:
            max_tokens = self.max_tokens
        
        # 重试循环
        last_error = None
        fixed_system_prompt = system_prompt  # 可能会被修复的 system prompt
        type_fix_attempted = False  # 是否已尝试类型修复
        
        for retry_count in range(max_retries + 1):
            if retry_count > 0:
                safe_print(f"   🔄 LLM重试 {retry_count}/{max_retries}...")
                time.sleep(2 * retry_count)  # 指数退避：2秒, 4秒, 6秒
                
                # 根据上次错误生成提示（帮助 LLM 避免重复错误）
                if last_error:
                    retry_hint = self._generate_retry_hint(last_error.error_information, retry_count)
                    if retry_hint:
                        fixed_system_prompt = system_prompt + "\n\n" + retry_hint
                        safe_print(f"   📝 添加错误提醒: {retry_hint[:80]}...")
            
            # 调用内部实现
            response = self._chat_internal(
                history, model, fixed_system_prompt, tool_list, 
                tool_choice, temperature, max_tokens
            )
            
            # 如果成功，直接返回
            if response.status == "success":
                if retry_count > 0 or type_fix_attempted:
                    safe_print(f"   ✅ 重试成功 (第{retry_count + 1}次尝试)")
                return response
            
            # 检查是否是工具参数类型错误（优先处理，不消耗重试次数）
            if not type_fix_attempted and ("did not match schema" in response.error_information or "expected array, but got string" in response.error_information):
                safe_print(f"   🔧 检测到工具参数类型错误，尝试自动修复...")
                
                # 尝试修复 system prompt（添加参数类型提示）
                fix_hint = self._generate_type_fix_hint(response.error_information)
                if fix_hint:
                    fixed_system_prompt = system_prompt + "\n\n" + fix_hint
                    safe_print(f"   📝 已添加参数类型提示，立即重试...")
                    type_fix_attempted = True
                    last_error = response
                    
                    # 立即重试，不计入retry_count
                    response = self._chat_internal(
                        history, model, fixed_system_prompt, tool_list, 
                        tool_choice, temperature, max_tokens
                    )
                    
                    if response.status == "success":
                        safe_print(f"   ✅ 参数类型修复成功！")
                        return response
                    else:
                        safe_print(f"   ⚠️ 修复后仍失败，继续常规重试...")
                        last_error = response
                        continue
            
            # 所有错误都重试（包括API余额不足、密钥错误等）
            error_type = self._get_error_type(response.error_information)
            safe_print(f"   ⚠️ {error_type} (第{retry_count + 1}次)")
            last_error = response
            
            if retry_count < max_retries:
                continue  # 继续重试
            else:
                # 达到最大重试次数，抛出异常（让上层捕获并触发错误处理）
                safe_print(f"   ❌ 已达到最大重试次数 ({max_retries + 1})")
                error_msg = f"LLM 调用失败（已重试 {max_retries + 1} 次）: {response.error_information}"
                raise Exception(error_msg)
                # return response
        
        # 所有重试都失败了
        safe_print(f"   ❌ LLM调用失败（已重试{max_retries + 1}次）")
        return last_error
    
    def _chat_internal(
        self,
        history: List[ChatMessage],
        model: str,
        system_prompt: str,
        tool_list: List[str],
        tool_choice: str,
        temperature: float,
        max_tokens: int
    ) -> LLMResponse:
        """
        LLM调用的内部实现（使用 LiteLLM 原生超时机制）
        """
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
                "stream": True,  # 启用流式模式
                # --- LiteLLM 原生超时设定（从配置文件读取）---
                "timeout": self.timeout,              # 建立连接及整体响应的最大等待时间（秒）
                "stream_timeout": self.stream_timeout,  # 两个流式数据块（chunk）之间的最大间隔时间（秒）
            }
            
            # 只在 base_url 非空时添加 api_base
            if self.base_url:
                kwargs["api_base"] = self.base_url
            
            # 只在max_tokens > 0时添加
            if max_tokens > 0:
                kwargs["max_tokens"] = max_tokens
            
            # 添加工具定义（只有当工具列表非空时才添加工具相关参数）
            if tools_definition:
                # 工具列表非空：正常添加工具参数
                kwargs["tools"] = tools_definition
                if tool_choice == "required":
                    kwargs["tool_choice"] = "required"
                kwargs["parallel_tool_calls"] = False
            # 注意：当 tools_definition 为空时，即使 tool_choice="none" 也不添加任何参数
            # 这避免了 API 错误：When using `tool_choice`, `tools` must be set
            
            # 添加模型特定的额外参数
            model_extra_params = self.model_configs.get(model, {})
            if model_extra_params:
                if "provider" in model_extra_params:
                    if "extra_body" not in kwargs:
                        kwargs["extra_body"] = {}
                    kwargs["extra_body"]["provider"] = model_extra_params["provider"]
                
                if "extra_headers" in model_extra_params:
                    kwargs["extra_headers"] = model_extra_params["extra_headers"]
                
                if "extra_body" in model_extra_params:
                    if "extra_body" not in kwargs:
                        kwargs["extra_body"] = {}
                    kwargs["extra_body"].update(model_extra_params["extra_body"])
            
            # 发起流式请求（LiteLLM 会根据 timeout 和 stream_timeout 自动管理超时）
            safe_print(f"   🌊 正在调用LLM (timeout={kwargs['timeout']}s, stream_timeout={kwargs['stream_timeout']}s)...")
            safe_print(f"   📨 请求模型: {model}")
            safe_print(f"   🛠️ 工具数量: {len(tools_definition)}")
            safe_print(f"   📝 消息数: {len(messages)}")
            request_start_time = time.time()
            
            # 累积变量
            accumulated_content = ""
            accumulated_tool_calls = {}  # index -> {id, name, arguments}
            finish_reason = "unknown"
            response_model = model
            
            chunk_count = 0
            
            # --- 强制首包超时检测（包含 completion 调用以防止连接池死锁）---
            try:
                # 定义完整的初始化和首包获取函数（防止 httpx 连接池锁死锁）
                def get_response_and_first_chunk():
                    iterator = completion(**kwargs)
                    first = next(iterator)
                    return iterator, first
                
                # 强制首包超时时间（秒），包含连接建立+首包接收，防止 httpx 连接池死锁
                first_chunk_timeout = self.first_chunk_timeout  # 从配置文件读取
                
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(get_response_and_first_chunk)
                    try:
                        # 强制等待整个初始化过程（包括 completion 调用）
                        response_iterator, first_chunk = future.result(timeout=first_chunk_timeout)
                        
                        # 处理首包
                        chunk_count += 1
                        latency = time.time() - request_start_time
                        safe_print(f"   ⚡️ 首包延迟: {latency:.2f}s")
                        
                        # 处理首包逻辑
                        if hasattr(first_chunk, 'model'):
                            response_model = first_chunk.model
                        
                        # 打印首包
                        # try:
                        #     safe_print(f"\n[chunk #1] {first_chunk}", flush=True)
                        # except Exception:
                        #     pass

                        if first_chunk.choices:
                            delta = first_chunk.choices[0].delta
                            if hasattr(delta, 'content') and delta.content:
                                accumulated_content += delta.content
                                # try:
                                #     safe_print(delta.content, end="", flush=True)
                                # except Exception:
                                #     pass
                            
                            if hasattr(delta, 'tool_calls') and delta.tool_calls:
                                for tc in delta.tool_calls:
                                    idx = tc.index
                                    if idx not in accumulated_tool_calls:
                                        accumulated_tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                                    if tc.id:
                                        accumulated_tool_calls[idx]["id"] = tc.id
                                    if tc.function and tc.function.name:
                                        accumulated_tool_calls[idx]["name"] += tc.function.name
                                    if tc.function and tc.function.arguments:
                                        accumulated_tool_calls[idx]["arguments"] += tc.function.arguments
                                    
                                    # try:
                                    #     tc_args_preview = tc.function.arguments[:200] if tc.function and tc.function.arguments else ""
                                    #     safe_print(f"\n[tool_call #{idx}] {tc.function.name}: {tc_args_preview}", flush=True)
                                    # except Exception:
                                    #     pass
                                        
                            if first_chunk.choices[0].finish_reason:
                                finish_reason = first_chunk.choices[0].finish_reason

                    except concurrent.futures.TimeoutError:
                        raise TimeoutError(f"连接建立或首包接收超时（超过 {first_chunk_timeout}s）- 可能原因：httpx连接池死锁、网络断开、服务器无响应")
            
            except StopIteration:
                safe_print("   ⚠️ 响应为空（无数据块）")
                return LLMResponse(
                    status="error",
                    output="",
                    tool_calls=[],
                    model=model,
                    finish_reason="empty",
                    error_information="Empty response - no chunks received"
                )
            
            # --- 继续处理剩余 chunk ---
            # 直接迭代流式响应（LiteLLM 会自动处理后续的 stream_timeout）
            for chunk in response_iterator:
                chunk_count += 1
                # 全量打印 chunk（方便观察断联和增量内容，可能较噪声）
                # try:
                #     safe_print(f"\n[chunk #{chunk_count}] {chunk}", flush=True)
                # except Exception:
                #     pass
                
                # 提取模型信息
                if hasattr(chunk, 'model'):
                    response_model = chunk.model
                
                if not chunk.choices:
                    continue
                    
                delta = chunk.choices[0].delta
                
                # A. 累积文本内容
                if hasattr(delta, 'content') and delta.content:
                    accumulated_content += delta.content
                    # 直接流式打印模型文本片段，便于无 CLI 时观察进度
                    try:
                        safe_print(delta.content, end="", flush=True)
                    except Exception:
                        pass
                
                # B. 累积工具调用
                if hasattr(delta, 'tool_calls') and delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in accumulated_tool_calls:
                            accumulated_tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                        
                        if tc.id:
                            accumulated_tool_calls[idx]["id"] = tc.id
                        if tc.function and tc.function.name:
                            accumulated_tool_calls[idx]["name"] += tc.function.name
                        if tc.function and tc.function.arguments:
                            accumulated_tool_calls[idx]["arguments"] += tc.function.arguments
                        
                        # 工具调用流式打印：名称与参数增量，便于无 CLI 时跟踪
                        # try:
                        #     tc_args_preview = tc.function.arguments[:200] if tc.function and tc.function.arguments else ""
                        #     safe_print(f"\n[tool_call #{idx}] {tc.function.name}: {tc_args_preview}", flush=True)
                        # except Exception:
                        #     pass
                
                # C. 记录结束原因
                if chunk.choices[0].finish_reason:
                    finish_reason = chunk.choices[0].finish_reason
            
            safe_print(f"   ✅ 流式响应完成，共接收 {chunk_count} 个数据块")
            
            # 构建最终的 ToolCall 对象列表
            final_tool_calls = []
            for idx in sorted(accumulated_tool_calls.keys()):
                tc_data = accumulated_tool_calls[idx]
                
                try:
                    args_str = tc_data["arguments"]
                    if not args_str:
                        args = {}
                    else:
                        args = json.loads(args_str)
                except json.JSONDecodeError as e:
                    safe_print(f"\n⚠️ 工具参数JSON解析失败: {str(e)}")
                    safe_print(f"   原始参数: {tc_data['arguments'][:200]}...")
                    
                    # 尝试修复常见的 JSON 错误
                    args = self._try_fix_json(tc_data["arguments"])
                    if args:
                        safe_print(f"   ✅ JSON 自动修复成功")
                    else:
                        safe_print(f"   ❌ JSON 修复失败，使用空参数")
                        args = {}
                
                final_tool_calls.append(ToolCall(
                    id=tc_data["id"] or f"call_{idx}",
                    name=tc_data["name"],
                    arguments=args
                ))
            
            return LLMResponse(
                status="success",
                output=accumulated_content,
                tool_calls=final_tool_calls,
                model=response_model,
                finish_reason=finish_reason
            )
        
        except Exception as e:
            # 捕获所有异常，包括 LiteLLM 抛出的超时异常
            error_msg = str(e)
            is_timeout = any(keyword in error_msg.lower() for keyword in ["timeout", "timed out", "time out"])
            
            if is_timeout:
                safe_print(f"⏱️  LLM调用超时 (原生超时机制)")
                safe_print(f"   超时详情: {error_msg}")
                safe_print(f"   💡 提示: 如果频繁超时，可能是：")
                safe_print(f"      1. 网络连接不稳定")
                safe_print(f"      2. 上下文过长导致 API 响应缓慢")
                safe_print(f"      3. API 服务商限流或过载")
            else:
                safe_print(f"❌ LLM调用异常: {error_msg}")
            
            # 返回包含详细错误信息的响应
            import traceback
            error_detail = traceback.format_exc()
            
            return LLMResponse(
                status="error",
                output="",
                tool_calls=[],
                model=model,
                finish_reason="timeout" if is_timeout else "error",
                error_information=f"{error_msg}\n\nDetails:\n{error_detail}"
            )
    
    def set_tools_config(self, tools_config: Dict):
        """
        设置工具配置（从ConfigLoader传入）
        
        Args:
            tools_config: 工具配置字典
        """
        self.tools_config = tools_config
    
    def _try_fix_json(self, json_str: str) -> Dict:
        """
        尝试修复常见的 JSON 格式错误
        
        Args:
            json_str: 可能有问题的 JSON 字符串
            
        Returns:
            解析后的字典，失败返回 None
        """
        if not json_str or not json_str.strip():
            return {}
        
        try:
            # 策略 1: 去除尾部多余的逗号
            fixed = json_str.strip()
            if fixed.endswith(',}'):
                fixed = fixed[:-2] + '}'
            if fixed.endswith(',]'):
                fixed = fixed[:-2] + ']'
            
            # 策略 2: 补全缺失的结束括号
            open_braces = fixed.count('{')
            close_braces = fixed.count('}')
            if open_braces > close_braces:
                fixed += '}' * (open_braces - close_braces)
            
            open_brackets = fixed.count('[')
            close_brackets = fixed.count(']')
            if open_brackets > close_brackets:
                fixed += ']' * (open_brackets - close_brackets)
            
            # 策略 3: 尝试解析
            result = json.loads(fixed)
            return result
        
        except Exception:
            # 所有修复策略都失败
            return None
    
    def _generate_type_fix_hint(self, error_info: str) -> str:
        """
        从错误信息中提取参数类型错误，生成修复提示
        
        Args:
            error_info: 错误信息字符串
            
        Returns:
            修复提示文本（添加到 system prompt）
        """
        try:
            import re
            
            # 提取工具名
            tool_match = re.search(r"tool (\w+) did not match", error_info)
            if not tool_match:
                return ""
            tool_name = tool_match.group(1)
            
            # 提取所有参数错误（支持多个参数同时出错）
            param_errors = re.findall(r"`/([\w_]+)`:\s*expected\s+(\w+),\s*but\s+got\s+(\w+)", error_info)
            
            if not param_errors:
                return ""
            
            # 分类处理
            null_params = []
            type_mismatches = []
            
            for param_name, expected_type, actual_type in param_errors:
                if actual_type == "null":
                    null_params.append(param_name)
                else:
                    type_mismatches.append((param_name, expected_type, actual_type))
            
            hints = []
            
            # 处理 null 值错误
            if null_params:
                params_str = "、".join(null_params)
                hints.append(f"""
⚠️ 参数 null 值错误：
工具 {tool_name} 的参数 {params_str} 被设置为 null

重要规则：
- 可选参数如果不需要，必须完全省略，不要传 null！
- 错误示例: {{"path": "file.txt", "start_line": null}}  ❌
- 正确示例: {{"path": "file.txt"}}  ✅
""")
            
            # 处理类型不匹配错误
            for param_name, expected_type, actual_type in type_mismatches:
                safe_print(f"   🔍 检测到: 工具 {tool_name}, 参数 {param_name}, 需要 {expected_type}, 得到 {actual_type}")
                
                if expected_type == "array" and actual_type == "string":
                    hints.append(f"""
⚠️ 参数类型错误：
工具 {tool_name} 的参数 {param_name} 必须是数组类型！
- 错误: {{"{param_name}": "value"}}  ❌
- 正确: {{"{param_name}": ["value"]}}  ✅
""")
                elif expected_type == "string" and actual_type == "array":
                    hints.append(f"""
⚠️ 参数类型错误：
工具 {tool_name} 的参数 {param_name} 必须是字符串类型！
- 错误: {{"{param_name}": ["value"]}}  ❌
- 正确: {{"{param_name}": "value"}}  ✅
""")
                else:
                    hints.append(f"""
⚠️ 参数类型错误：
工具 {tool_name} 的参数 {param_name} 需要 {expected_type}，实际得到 {actual_type}
""")
            
            return "\n".join(hints) if hints else ""
        
        except Exception as e:
            safe_print(f"   ⚠️ 生成修复提示失败: {e}")
            return ""
    
    def _get_error_type(self, error_info: str) -> str:
        """
        从错误信息中提取友好的错误类型描述
        
        Args:
            error_info: 错误信息字符串
            
        Returns:
            友好的错误类型描述
        """
        if "timeout" in error_info.lower() or "timed out" in error_info.lower():
            return "连接超时"
        elif "Internal Server Error" in error_info:
            return "服务器内部错误"
        elif "Failed to parse" in error_info and "JSON" in error_info:
            return "JSON格式错误"
        elif "expected integer, but got null" in error_info:
            return "参数null值错误"
        elif "expected array, but got string" in error_info:
            return "参数类型错误(string→array)"
        elif "expected string, but got array" in error_info:
            return "参数类型错误(array→string)"
        elif "did not match schema" in error_info:
            return "参数校验失败"
        elif "not in request.tools" in error_info:
            return "工具不存在错误"
        elif "Invalid API key" in error_info or "api_key" in error_info.lower():
            return "API密钥错误"
        elif "rate limit" in error_info.lower():
            return "速率限制"
        elif "insufficient" in error_info.lower() or "quota" in error_info.lower():
            return "余额不足"
        else:
            return "未知错误"
    
    def _generate_retry_hint(self, error_info: str, retry_count: int) -> str:
        """
        根据错误信息生成重试提示（添加到 system prompt）
        
        Args:
            error_info: 错误信息字符串
            retry_count: 当前重试次数
            
        Returns:
            重试提示文本
        """
        import re
        
        # 1. 服务器错误 - 静默重试（不需要提示 LLM）
        if "Internal Server Error" in error_info:
            return ""
        
        # 2. null 值错误 - 最常见
        if "but got null" in error_info:
            # 尝试提取所有 null 参数
            null_params = re.findall(r"`/([\w_]+)`:\s*expected\s+\w+,\s*but\s+got\s+null", error_info)
            if null_params:
                params_str = "、".join(null_params)
                hint = f"""
⚠️ 第{retry_count}次重试警告：
上次调用失败！原因：参数 {params_str} 被设置为 null

重要规则：
- 可选参数如果不需要，必须完全省略，不要传递 null 值！
- 错误示例: {{"path": "file.txt", "start_line": null}}  ❌
- 正确示例: {{"path": "file.txt"}}  ✅ (直接省略 start_line)

请重新生成工具调用，确保不传递 null 值。
"""
                return hint
        
        # 3. JSON 解析错误
        if "Failed to parse" in error_info and "JSON" in error_info:
            hint = f"""
⚠️ 第{retry_count}次重试警告：
上次调用失败！原因：工具参数 JSON 格式错误

JSON 格式要求：
- 所有键名必须用双引号：{{"key": "value"}}  ✅  {{key: "value"}}  ❌
- 字符串值必须用双引号：{{"path": "file.txt"}}  ✅  {{"path": 'file.txt'}}  ❌
- 不要有尾部逗号：{{"a": 1, "b": 2}}  ✅  {{"a": 1, "b": 2,}}  ❌
- 特殊字符需要转义：{{"path": "C:\\\\file.txt"}}  ✅

请重新生成工具调用，确保 JSON 格式正确。
"""
            return hint
        
        # 4. 工具不存在错误
        if "not in request.tools" in error_info:
            # 尝试提取工具名
            tool_match = re.search(r"attempted to call tool ['\"](\w+)['\"]", error_info)
            wrong_tool = tool_match.group(1) if tool_match else "某个工具"
            
            hint = f"""
⚠️ 第{retry_count}次重试警告：
上次调用失败！原因：尝试调用不存在的工具 '{wrong_tool}'

重要规则：
- 只能调用提供的工具列表中的工具
- 不要自己发明或假设存在某个工具
- 仔细检查可用工具列表

请重新生成工具调用，只使用已提供的工具。
"""
            return hint
        
        # 5. 类型不匹配（array vs string）
        if "expected array, but got string" in error_info:
            tool_match = re.search(r"tool (\w+) did not match", error_info)
            param_match = re.search(r"`/([\w_]+)`:\s*expected array", error_info)
            
            tool_name = tool_match.group(1) if tool_match else "某工具"
            param_name = param_match.group(1) if param_match else "某参数"
            
            hint = f"""
⚠️ 第{retry_count}次重试警告：
上次调用失败！原因：工具 {tool_name} 的参数 {param_name} 类型错误

类型要求：
- 参数 {param_name} 必须是数组（array）类型
- 错误示例: {{"{param_name}": "value"}}  ❌
- 正确示例: {{"{param_name}": ["value"]}}  ✅

请重新生成工具调用，使用数组格式（方括号包裹）。
"""
            return hint
        
        # 6. API 余额/密钥错误 - 也给提示（虽然重试可能无效）
        if "insufficient" in error_info.lower() or "quota" in error_info.lower():
            hint = f"""
⚠️ 第{retry_count}次重试警告：
上次调用失败！原因：API 余额不足或配额已用尽

这可能是临时问题，正在重试...
如果持续失败，请检查 API 账户状态。
"""
            return hint
        
        if "Invalid API key" in error_info or "api_key" in error_info.lower():
            hint = f"""
⚠️ 第{retry_count}次重试警告：
上次调用失败！原因：API 密钥错误或无效

这可能是临时问题，正在重试...
如果持续失败，请检查 API 密钥配置。
"""
            return hint
        
        # 7. 通用提示
        hint = f"""
⚠️ 第{retry_count}次重试警告：
上次调用失败！错误信息：{error_info[:200]}

请仔细检查工具调用的格式、参数类型和值，确保符合工具定义。
"""
        return hint
    
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
        history = [ChatMessage(role="user", content="详细描述这张图，如果要点击验证，坐标是什么")]
        response = client.chat(
            history=history,
            model=client.models[0],  # 使用第一个可用模型
            system_prompt="你是一个AI助手，请使用工具来完成任务。 图片：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GLXWM2/evKCgoMLCwobfGl2nVqvVDx48aNeunThorIdarU5NTW3Xrl23bt20W5jW9+rV6ynvserq6rS0tI4dOxobGz9ZCaWlpfn5+b169XqaagAAAABAC/e0ATDQnGftABigWWpqaoYNG9a+ffujR482ukNJSUmHDh0WL15MT6EDAAAAAMCTecEjhwDAGJs+fXq95961/fTTT4wxwx8pBwAAAACARmEE+BkoKioqKirC5Ex4Th49elReXo4bDAAAAADgKSEABgAAAAAAgBah6dcgAQAAAAAAAPwNIAAGAAAAAACAFgEBMAAAAAAAALQICIABAAAAAACgRUAADAAAAAAAAC2CzgBYEIRbt26VlJQ8TekajSY9Pb2goOBpCnm20tPTc3JyXnQt/obKy8tv3rypVqv/4GMB9Lt9+/bjx4+f6ylUKpV4A+fm5qampj7X0wEAAADAE9MZABcUFLzzzjtRUVFPVm50dPSAAQPatGljYWHB87xUKl24cKFKpXqKqj4bzs7Oq1atanRTXl5enz59Fi9e/DTlV1dXHz58+O8ay40cOXLQoEF0dYIgxMTEPHjwgDZFRUX169cvPz/fkHKe5lj466rX738MhUIRHBz8zIutrKyUSCRbt25ljJ0+fbpfv36FhYWMsbVr1zo5OT3z0wEAAADAM/Hsp0ALguDl5TVkyJCJEyfm5eWVlpbW1taeP38+ISFBoVAUFxc/8zM+K7W1tSkpKSkpKU9TyP79+11dXWtqap5dvf5E7t69e/36dQqA09LSBg0a9GSDXU9zLPx1/Z36vW3btoyxV1555UVXBAAAAACaoc0zL/GHH34ICAi4deuWQqG4e/fuzz//3KFDh8GDB2/btm3hwoVLly4NDAx85id9JmQyWXFxMcdxT1+URCJ5FjX600lKStJoNNo/+p/mSv+urQT6/T36vXXr1oyxV1999UVXBAAAAACaoYkR4Orq6nnz5hkZGUml0tmzZ5eXlzPGiouLlUplWFjY559/bmRkZGRkNHv2bJreXFpaOm/evA0bNigUirCwsLfffnvTpk1ffvll//79ly9fvnHjxm3btmk0mhMnTiiVSu2BoGvXrimVytjYWJqKPGnSJCMjIxMTk4ULF16+fFncLTk5eeTIkVKp1MjI6JNPPsnOzqb1Fy5cUCqVJ06cGDBggFQqdXZ2vnv37r1790aM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6NDSpUvnz5+/cePGsy/97LPPrlu3buPGjUePHv3FL37x0ksvbdq0KZfL3XfffVVVVZs3b966devmzZt/9atfrV69esmSJb/5zW+OHDkyZ86chx9++OTJk/PmzduwYcPLL7+8Y8eOqVOnzpw5s7GxMfvkc2aJM1u2bPnrX/+6d+/eG2+8cenSpYMGDfrTn/6UUtq9e/eUKVP+g98cAACAy+CiA/i1115LKT3yyCMDBw78+Mc//pe//KW2tjZ76ZFHHrn99ts3bNiQUpoyZcrmzZuLioomT5784osvppSWLl36u9/9rqOj4+9//3s2c3vOblIvvvhidXX1dddd9/rrr48ePbq0tLS+vv7sE+6///6GhoaKiorXXnstu4m6tbX14MGDu3btmj179g033FBZWbl27doZM2a8+uqr48aNmzFjRr9+/ebOnbt69epcLvf73/9+1qxZEyZMGDJkyDe/+c2U0t/+9rcLfNM5c+aMHz++rKzsnnvuaWpqutgfCgAAgCtK0cW+Yffu3ZWVleXl5dnTj370o9mD6urqwsLClFK2OnfBggX5t1x33XUppYaGhq9//euHDh2qrq6+/vrr3z3v+q9//au5uXnevHnZ0xEjRpxzQktLy4MPPvj6669XVFR87GMfy5Yiv/XWWymlcePGZeeMHj06pbRjx466urrsSL9+/W655ZZssvfmm2/ODo4aNSql9Pbbb2cP8rIyzwwePDh7MGDAgLOPAwAAcDW66AAeNmzYoUOHjh49WlFRkVL67ne/e+utt6aUior++6NGjBiRUlq7dm0WyQ0NDZ2dnWfOnHnggQemT5++aNGisrKyFStWrF+/vqen5+xPvv7667u7u1955ZXs6SuvvJJ9VN6CBQu6uro2bdpUVVW1b9++bF536NCh+aW8KaUVK1Y0NzePGDEif/v0yZMnFyxY8NBDD2XT19mq4+3bt6eUamtrCwoKUkrZsuGU0r59+/7dXxIAAIAr2kXfAn3nnXemlJ588snGxsbnn3/+Rz/6UVVV1dkn3H333SmlhQsXHjx4cNOmTZ/61KdeffXVrHWvvfbakpKSnTt3LlmyJH8LdGlpaUNDQ3Nz87Rp0zZv3vzrX//62LFjK1asmD59en6NbqaoqGjAgAGDBg1qaWn5zne+k31CTU3NmDFjli1b9uabb27ZsmXevHllZWX33nvvjh07li9f3tzc/MMf/vDll18eNWrUZz/72eXLl//5z3/es2fPD37wg9LS0ptvvnnYsGEppd/+9rdNTU0/+9nP8rthnVdpaWlK6R//+McFNqkGAADgynTRATxy5Mjly5c/88wzY8eO/fa3v/3kk09mdx3nVVdXP/vss/X19ePHj//0pz89derUuXPnFhcXL1myZOXKlUOHDv3EJz7xuc99LttlKqU0ffr073//+4sXL54+ffr8+fMfffTRUaNGLVy4cNGiRbfddtvZnzx37tyOjo6qqqra2trKysrq6uqGhoaioqKnnnrq2LFjkydP/uQnPzlr1qz77rtv6tSp3/rWtx5++OHRo0dv2LBh2bJlxcXFixcvrqur+8xnPnPLLbds27Zt7dq1gwYNGjJkyPz585csWTJmzJg1a9acc8V3f/cxY8bcf//9f/jDHy72dwMAAODy6nPeDZBTSo899tjjjz/e29tyuVxTU9OQIUP69OnT2zmNjY3l5eX5W6OzJbvZu7Ibj/NOnjxZUlKSnXnmzJnm5uZsYva8mpqaysrKiouLzzne0tJSWlrar1+//JF33nnn+PHj2a3aee3t7Z2dndmy5LzOzs62trb8ot8LyOVyJ06c+NCHPvTuL75+/fr+/ftnE+AAAABcaS56DXCmT58+2eLbC3j3CQUFBect24EDB/7vgIqKLlC/KaUhQ4ac93h+X668vn37nlO/KaWSkpKSkpJzDvbv379///4XuGhenz59ysrK3suZAAAAXFEu+hZoAAAAuBoJYAAAAELoNYALCgpOnz59aQdzdWtvb3/3ymQAAACuEL2uAb7rrruee+65mpqaSzueq1V7e/v27dvvvffeyz0QAAAAzq/XXaBTSlu3bj18+PClHc/Vqri4eMKECe/eXgsAAIArxIUCGAAAAD4wbIIFAABACAIYAACAEAQwAAAAIQhgAAAAQhDAAAAAhCCAAQAACEEAAwAAEIIABgAAIAQBDAAAQAgCGAAAgBAEMAAAACEIYAAAAEIQwAAAAIQggAEAAAhBAAMAABCCAAYAACAEAQwAAEAIAhgAAIAQBDAAAAAhCGAAAABCEMAAAACEIIABAAAIQQADAAAQggAGAAAgBAEMAABACAIYAACAEAQwAAAAIQhgAAAAQhDAAAAAhCCAAQAACEEAAwAAEIIABgAAIAQBDAAAQAgCGAAAgBAEMAAAACEIYAAAAEIQwAAAAIQggAEAAAhBAAMAABCCAAYAACAEAQwAAEAIAhgAAIAQBDAAAAAhCGAAAABCEMAAAACEIIABAAAIQQADAAAQggAGAAAgBAEMAABACAIYAACAEAQwAAAAIQhgAAAAQhDAAAAAhCCAAQAACEEAAwAAEIIABgAAIAQBDAAAQAgCGAAAgBAEMAAAACEIYAAAAEIQwAAAAIQggAEAAAhBAAMAABCCAAYAACAEAQwAAEAIAhgAAIAQBDAAAAAhCGAAAABCEMAAAACEIIABAAAIQQADAAAQggAGAAAgBAEMAABACAIYAACAEAQwAAAAIQhgAAAAQhDAAAAAhCCAAQAACEEAAwAAEIIABgAAIAQBDAAAQAgCGAAAgBAEMAAAACEIYAAAAEIQwAAAAIQggAEAAAhBAAMAABCCAAYAACAEAQwAAEAIAhgAAIAQBDAAAAAhCGAAAABCEMAAAACEIIABAAAIQQADAAAQggAGAAAgBAEMAABACAIYAACAEAQwAAAAIQhgAAAAQhDAAAAAhCCAAQAACEEAAwAAEIIABgAAIAQBDAAAQAgCGAAAgBAEMAAAACEIYAAAAEIQwAAAAIQggAEAAAhBAAMAABCCAAYAACAEAQwAAEAIAhgAAIAQBDAAAAAhCGAAAABCEMAAAACEIIABAAAIQQADAAAQggAGAAAgBAEMAABACAIYAACAEAQwAAAAIQhgAAAAQhDAAAAAhCCAAQAACKpaxowAAAfsSURBVEEAAwAAEIIABgAAIAQBDAAAQAgCGAAAgBAEMAAAACEIYAAAAEIQwAAAAIQggAEAAAhBAAMAABCCAAYAACAEAQwAAEAIAhgAAIAQBDAAAAAhCGAAAABCEMAAAACEIIABAAAIQQADAAAQggAGAAAgBAEMAABACAIYAACAEAQwAAAAIQhgAAAAQhDAAAAAhCCAAQAACEEAAwAAEIIABgAAIAQBDAAAQAgCGAAAgBAEMAAAACEIYAAAAEIQwAAAAIQggAEAAAhBAAMAABCCAAYAACAEAQwAAEAIAhgAAIAQBDAAAAAhCGAAAABCEMAAAACEIIABAAAIQQADAAAQggAGAAAgBAEMAABACAIYAACAEAQwAAAAIQhgAAAAQhDAAAAAhCCAAQAACEEAAwAAEIIABgAAIAQBDAAAQAgCGAAAgBAEMAAAACEIYAAAAEIQwAAAAIQggAEAAAhBAAMAABCCAAYAACAEAQwAAEAIAhgAAIAQBDAAAAAhCGAAAABCEMAAAACEIIABAAAIQQADAAAQggAGAAAgBAEMAABACAIYAACAEAQwAAAAIQhgAAAAQhDAAAAAhCCAAQAACEEAAwAAEIIABgAAIAQBDAAAQAgCGAAAgBAEMAAAACEIYAAAAEIQwAAAAIQggAEAAAhBAAMAABCCAAYAACAEAQwAAEAIAhgAAIAQBDAAAAAhCGAAAABCEMAAAACEIIABAAAIQQADAAAQggAGAAAgBAEMAABACAIYAACAEAQwAAAAIQhgAAAAQhDAAAAAhCCAAQAACEEAAwAAEIIABgAAIAQBDAAAQAgCGAAAgBAEMAAAACEIYAAAAEIQwAAAAIQggAEAAAhBAAMAABCCAAYAACAEAQwAAEAIAhgAAIAQBDAAAAAhCGAAAABCEMAAAACEIIABAAAIQQADAAAQggAGAAAgBAEMAABACAIYAACAEAQwAAAAIQhgAAAAQhDAAAAAhCCAAQAACEEAAwAAEIIABgAAIAQBDAAAQAgCGAAAgBAEMAAAACEIYAAAAEIQwAAAAIQggAEAAAhBAAMAABCCAAYAACAEAQwAAEAIAhgAAIAQBDAAAAAhCGAAAABCEMAAAACEIIABAAAIQQADAAAQggAGAAAgBAEMAABACAIYAACAEAQwAAAAIQhgAAAAQhDAAAAAhCCAAQAACEEAAwAAEIIABgAAIAQBDAAAQAgCGAAAgBAEMAAAACEIYAAAAEIQwAAAAIQggAEAAAhBAAMAABCCAAYAACAEAQwAAEAIAhgAAIAQBDAAAAAhCGAAAABCEMAAAACEIIABAAAIQQADAAAQggAGAAAgBAEMAABACAIYAACAEAQwAAAAIQhgAAAAQhDAAAAAhCCAAQAACEEAAwAAEIIABgAAIAQBDAAAQAgCGAAAgBAEMAAAACEIYAAAAEIQwAAAAIQggAEAAAhBAAMAABCCAAYAACAEAQwAAEAIAhgAAIAQBDAAAAAhCGAAAABCEMAAAACEIIABAAAIQQADAAAQggAGAAAgBAEMAABACAIYAACAEAQwAAAAIQhgAAAAQhDAAAAAhCCAAQAACEEAAwAAEIIABgAAIAQBDAAAQAgCGAAAgBAEMAAAACEIYAAAAEIQwAAAAIQggAEAAAhBAAMAABCCAAYAACAEAQwAAEAIAhgAAIAQBDAAAAAhCGAAAABCEMAAAACEIIABAAAIQQADAAAQggAGAAAgBAEMAABACAIYAACAEAQwAAAAIQhgAAAAQhDAAAAAhCCAAQAACEEAAwAAEIIABgAAIAQBDAAAQAgCGAAAgBAEMAAAACEIYAAAAEIQwAAAAIQggAEAAAhBAAMAABCCAAYAACAEAQwAAEAIAhgAAIAQBDAAAAAhCGAAAABCEMAAAACEIIABAAAIQQADAAAQggAGAAAgBAEMAABACAIYAACAEAQwAAAAIQhgAAAAQhDAAAAAhCCAAQAACEEAAwAAEIIABgAAIAQBDAAAQAgCGAAAgBAEMAAAACEIYAAAAEIQwAAAAIQggAEAAAhBAAMAABCCAAYAACAEAQwAAEAIAhgAAIAQBDAAAAAhCGAAAABCEMAAAACEIIABAAAIQQADAAAQggAGAAAgBAEMAABACAIYAACAEAQwAAAAIQhgAAAAQhDAAAAAhCCAAQAACEEAAwAAEIIABgAAIAQBDAAAQAgCGAAAgBAEMAAAACEIYAAAAEIQwAAAAIQggAEAAAhBAAMAABCCAAYAACAEAQwAAEAIAhgAAIAQBDAAAAAhCGAAAABCEMAAAACEIIABAAAIQQADAAAQggAGAAAgBAEMAABACAIYAACAEAQwAAAAIQhgAAAAQhDAAAAAhCCAAQAACEEAAwAAEIIABgAAIAQBDAAAQAgCGAAAgBAEMAAAACEIYAAAAEIQwAAAAIQggAEAAAhBAAMAABCCAAYAACAEAQwAAEAIAhgAAIAQBDAAAAAhCGAAAABCEMAAAACEIIABAAAIQQADAAAQggAGAAAgBAEMAABACAIYAACAEAQwAAAAIQhgAAAAQhDAAAAAhCCAAQAACEEAAwAAEIIABgAAIAQBDAAAQAgCGAAAgBAEMAAAACEIYAAAAEIQwAAAAIQggAEAAAhBAAMAABCCAAYAACAEAQwAAEAIAhgAAIAQBDAAAAAhCGAAAABCEMAAAACEIIABAAAIQQADAAAQggAGAAAghP8CWX6GJ/CX6xQAAAAASUVORK5CYII=",
            tool_list=[],
            tool_choice="None"
        )
        
        safe_print(f"✅ 响应状态: {response.status}")
        safe_print(f"✅ 工具调用数量: {len(response.output)}")
        if response.tool_calls:
            safe_print(f"✅ 第一个工具: {response.tool_calls[0].name}")
    except Exception as e:
        safe_print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
