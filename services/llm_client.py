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
import threading
import queue
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
                # 达到最大重试次数，返回最后的错误
                safe_print(f"   ❌ 已达到最大重试次数 ({max_retries + 1})")
                return response
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
        LLM调用的内部实现（单次尝试，不含重试逻辑）
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
                "stream": True,  # 关键：强制开启流式模式
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
                    kwargs["tool_choice"] = "required"
                kwargs["parallel_tool_calls"] = False
            elif tool_choice == "none":
                # 明确告诉模型不要使用工具（即使没有提供工具列表）
                kwargs["tool_choice"] = "none"
            
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
                
                # safe_print(f"   ⚙️  应用模型额外参数: {list(model_extra_params.keys())}")
            
            # 添加调试信息
            # safe_print(f"   📝 System Prompt长度: {len(system_prompt)} 字符")
            # safe_print(f"   🔧 工具数量: {len(tools_definition)}")
            # safe_print(f"   📨 消息数量: {len(messages)}")
            # safe_print(f"   🌊 流式调用LLM (监控假死)...")
            
            # 添加 timeout（防止建立连接阶段卡死）
            # litellm 使用 timeout 参数（不是 request_timeout！）
            kwargs["timeout"] = 60  # 60秒总超时（包括建立连接 + 第一个响应）
            # safe_print("📝,正式请求（timeout=60秒）")
            # 1. 发起流式请求
            response_iterator = completion(**kwargs)
            
            # safe_print("📝,正式请求2")
            # 2. 累积变量
            accumulated_content = ""
            accumulated_tool_calls = {}  # index -> {id, name, arguments}
            finish_reason = "unknown"
            response_model = model
            
            # 3. 监控配置
            STREAM_TIMEOUT = 45  # 45秒无数据则判定为假死
            
            # 线程共享状态
            shared_state = {
                "last_chunk_time": time.time(),
                "finished": False
            }
            
            # 使用队列解耦消费者和生成器
            chunk_queue = queue.Queue()
            
            def consume_stream():
                """后台线程：消费生成器并将数据放入队列"""
                try:
                    for chunk in response_iterator:
                        if shared_state["finished"]:
                            break
                        chunk_queue.put(chunk)
                    chunk_queue.put(None)  # 结束标志
                except Exception as e:
                    if not shared_state["finished"]:
                        chunk_queue.put(e)
            
            # 启动消费线程
            consumer_thread = threading.Thread(target=consume_stream, daemon=True)
            consumer_thread.start()
            
            # 4. 消费流（主循环：从队列读取并监控超时）
            chunk_count = 0
            
            while True:
                try:
                    # safe_print(f"📝time 1")
                    
                    # 1秒超时，定期检查是否假死
                    item = chunk_queue.get(timeout=0.5)
                    
                    if item is None:  # 正常结束
                        break
                    
                    if isinstance(item, Exception):  # 发生错误
                        raise item
                    
                    # 收到数据，重置计时器
                    shared_state["last_chunk_time"] = time.time()
                    chunk = item
                    chunk_count += 1
                    
                    # 提取模型信息
                    if hasattr(chunk, 'model'):
                        response_model = chunk.model
                    
                    if not chunk.choices:
                        continue
                        
                    delta = chunk.choices[0].delta
                    
                    # A. 累积文本内容
                    if hasattr(delta, 'content') and delta.content:
                        accumulated_content += delta.content
                        # safe_print(f"📝{delta.content}", flush=True, end='')
                    
                    # B. 累积工具调用
                    if hasattr(delta, 'tool_calls') and delta.tool_calls:
                        for tc in delta.tool_calls:
                            
                            idx = tc.index
                            if idx not in accumulated_tool_calls:
                                accumulated_tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                            # safe_print(f"\n📝 [TC #{idx}] ", flush=True, end='')
                            if tc.id:
                                accumulated_tool_calls[idx]["id"] = tc.id
                            if tc.function and tc.function.name:
                                accumulated_tool_calls[idx]["name"] += tc.function.name
                            if tc.function and tc.function.arguments:
                                accumulated_tool_calls[idx]["arguments"] += tc.function.arguments
                                # 统一流式输出：工具参数也直接输出
                                # safe_print(f"{tc.function.arguments}", flush=True, end='')
                    
                    # C. 记录结束原因
                    if chunk.choices[0].finish_reason:
                        finish_reason = chunk.choices[0].finish_reason
                        
                except queue.Empty:
                    # 队列超时（1秒无数据），检查是否总超时
                    elapsed = time.time() - shared_state["last_chunk_time"]
                    if elapsed > STREAM_TIMEOUT:
                        shared_state["finished"] = True
                        raise TimeoutError(f"LLM连接假死: 超过 {STREAM_TIMEOUT} 秒未收到数据")
                    continue
            
            # 标记完成
            shared_state["finished"] = True
            
            # 流式输出完成后换行
            safe_print("")  # 换行，分隔LLM输出和后续日志
            
            # 5. 构建最终的 ToolCall 对象列表
            final_tool_calls = []
            for idx in sorted(accumulated_tool_calls.keys()):
                tc_data = accumulated_tool_calls[idx]
                
                try:
                    # 解析完整的 JSON 参数字符串
                    args_str = tc_data["arguments"]
                    if not args_str:
                        args = {}
                    else:
                        args = json.loads(args_str)
                except json.JSONDecodeError as e:
                    # 仅在解析失败时输出错误信息
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
            
            # 6. 返回标准响应对象
            return LLMResponse(
                status="success",
                output=accumulated_content,
                tool_calls=final_tool_calls,
                model=response_model,
                finish_reason=finish_reason,
                usage=None  # 流式模式下 usage 信息需特殊处理，暂置空
            )
        
        except TimeoutError as e:
            # 捕获我们自己抛出的超时
            safe_print(f"⏱️  LLM超时: {e}")
            return LLMResponse(
                status="error",
                output="",
                tool_calls=[],
                model=model,
                finish_reason="timeout",
                error_information=str(e)
            )
            
        except Exception as e:
            import traceback
            error_detail = traceback.format_exc()
            safe_print(f"❌ LLM调用异常: {e}")
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
