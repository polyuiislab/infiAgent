#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vision分析工具 - 图片内容分析 & 图片读取（多模态支持）
"""

import base64
from pathlib import Path
from typing import Dict, Any

from .file_tools import BaseTool, get_abs_path

# 导入llm_client_lite
import sys
import os
# 添加父目录到路径
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)
from llm_client_lite import get_llm_client


class ImageReadTool(BaseTool):
    """
    图片读取工具 - 支持双模式：
    1. multimodal 模式：读取图片转 base64，嵌入到主模型 messages 中（主模型直接看图）
    2. text-only 模式：调用 Vision LLM 分析图片，返回文字描述
    
    mode 由 ToolServer 启动时从 llm_config.yaml 的 multimodal 字段读取决定
    """
    
    def __init__(self):
        """初始化"""
        super().__init__()
    
    @property
    def multimodal(self) -> bool:
        """从 llm_config.yaml 读取 multimodal 配置（每次读取，不缓存，确保配置修改后即时生效）"""
        try:
            import yaml
            config_path = Path(__file__).parent.parent.parent / "config" / "run_env_config" / "llm_config.yaml"
            if config_path.exists():
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f)
                return config.get("multimodal", False)
        except Exception:
            pass
        return False
    
    def execute(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行图片读取（支持多张图片）
        
        Parameters:
            image_paths (list[str]): 图片文件相对路径数组（相对于任务目录），读单图传单元素数组
            query (str, optional): 要问的问题（text-only 模式下传给 Vision LLM，
                                   multimodal 模式下作为元信息由 agent_executor 嵌入 user 消息）
            save_path (str, optional): 保存分析结果的相对路径（仅 text-only 模式有效）
        
        Returns:
            status: "success" 或 "error"
            output: 分析结果文本
            _image_base64_list: base64 编码的图片 data URI 列表（仅 multimodal 模式）
            _multimodal: 是否为多模态模式（仅 multimodal 模式）
        """
        try:
            image_paths = parameters.get("image_paths")
            query = parameters.get("query", "请描述这些图片的内容")
            save_path = parameters.get("save_path")
            
            if not image_paths:
                return {
                    "status": "error",
                    "output": "",
                    "error": "缺少必需参数: image_paths（字符串数组）"
                }
            
            if isinstance(image_paths, str):
                image_paths = [image_paths]
            
            # 验证所有路径
            abs_paths = []
            for p in image_paths:
                abs_p = get_abs_path(task_id, p)
                if not abs_p.exists():
                    return {
                        "status": "error",
                        "output": "",
                        "error": f"图片文件不存在: {p}"
                    }
                abs_paths.append((abs_p, p))
            
            # 根据多模态配置选择模式
            if self.multimodal:
                return self._execute_multimodal_batch(abs_paths, query)
            else:
                # text-only 模式：逐张分析（每张都调用 Vision LLM）
                all_results = []
                for abs_p, rel_p in abs_paths:
                    result = self._execute_text_only(abs_p, rel_p, query, task_id, save_path)
                    if result["status"] == "error":
                        return result
                    all_results.append(f"[{rel_p}]\n{result['output']}")
                
                return {
                    "status": "success",
                    "output": "\n\n".join(all_results),
                    "error": ""
                }
        
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": f"执行失败: {str(e)}"
            }
    
    # 图片分辨率限制（长边最大像素数）
    MAX_IMAGE_DIMENSION = 1568  # OpenAI/Anthropic 推荐的最大尺寸
    MAX_IMAGE_BYTES = 4 * 1024 * 1024  # base64 前的原始字节上限 4MB
    JPEG_QUALITY = 85  # JPEG 压缩质量
    
    def _compress_single_image(self, abs_image_path: Path) -> tuple:
        """
        压缩单张图片，返回 (data_uri, info_str)
        """
        try:
            from PIL import Image
            import io
            
            img = Image.open(abs_image_path)
            original_size = img.size
            
            # 转换色彩模式
            if img.mode in ('RGBA', 'P', 'LA'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                img = background
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            
            # 缩放
            width, height = img.size
            max_dim = self.MAX_IMAGE_DIMENSION
            resized = False
            if width > max_dim or height > max_dim:
                if width > height:
                    new_width = max_dim
                    new_height = int(height * max_dim / width)
                else:
                    new_height = max_dim
                    new_width = int(width * max_dim / height)
                img = img.resize((new_width, new_height), Image.LANCZOS)
                resized = True
            
            # 编码为 JPEG
            buffer = io.BytesIO()
            img.save(buffer, format='JPEG', quality=self.JPEG_QUALITY, optimize=True)
            image_data = buffer.getvalue()
            
            # 二次压缩
            if len(image_data) > self.MAX_IMAGE_BYTES:
                for quality in [70, 55, 40]:
                    buffer = io.BytesIO()
                    img.save(buffer, format='JPEG', quality=quality, optimize=True)
                    image_data = buffer.getvalue()
                    if len(image_data) <= self.MAX_IMAGE_BYTES:
                        break
            
            image_base64 = base64.b64encode(image_data).decode('utf-8')
            data_uri = f"data:image/jpeg;base64,{image_base64}"
            
            final_size = img.size
            size_kb = len(image_data) / 1024
            resize_info = f", resized from {original_size} to {final_size}" if resized else ""
            info = f"{final_size[0]}x{final_size[1]}, {size_kb:.0f}KB{resize_info}"
            
            return data_uri, info
            
        except ImportError:
            # PIL 不可用，直接读取
            with open(abs_image_path, 'rb') as f:
                image_data = f.read()
            image_base64 = base64.b64encode(image_data).decode('utf-8')
            suffix = abs_image_path.suffix.lower()
            mime_types = {
                '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.png': 'image/png', '.gif': 'image/gif',
                '.webp': 'image/webp', '.bmp': 'image/bmp'
            }
            mime_type = mime_types.get(suffix, 'image/jpeg')
            data_uri = f"data:{mime_type};base64,{image_base64}"
            size_kb = len(image_data) / 1024
            return data_uri, f"{size_kb:.0f}KB, no compression"
    
    def _execute_multimodal_batch(self, abs_paths: list, query: str) -> Dict[str, Any]:
        """
        多模态模式（批量）：读取多张图片，压缩，转 base64 列表
        
        Args:
            abs_paths: [(abs_path, rel_path), ...] 绝对路径和相对路径的元组列表
            query: 用户查询
        """
        try:
            data_uri_list = []
            output_parts = []
            
            for abs_p, rel_p in abs_paths:
                data_uri, info = self._compress_single_image(abs_p)
                data_uri_list.append(data_uri)
                output_parts.append(f"{rel_p} ({info})")
            
            output_msg = f"Loaded {len(data_uri_list)} image(s) in multimodal mode: {'; '.join(output_parts)}. Images are embedded in the conversation."
            
            return {
                "status": "success",
                "output": output_msg,
                "_image_base64_list": data_uri_list,
                "_multimodal": True,
                "error": ""
            }
        
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": f"读取图片失败: {str(e)}"
            }
    
    def _execute_text_only(self, abs_image_path: Path, image_path: str, query: str, 
                           task_id: str, save_path: str = None) -> Dict[str, Any]:
        """
        Text-only 模式：调用 Vision LLM 分析图片，返回文字描述
        """
        try:
            llm_client = get_llm_client()
            
            result = llm_client.vision_query(
                image_path=str(abs_image_path),
                question=query
            )
            
            # 保存分析结果
            if save_path:
                abs_save_path = get_abs_path(task_id, save_path)
                abs_save_path.parent.mkdir(parents=True, exist_ok=True)
                with open(abs_save_path, 'w', encoding='utf-8') as f:
                    f.write(result)
                output = f"结果保存在 {save_path}"
            else:
                output = result
            
            return {
                "status": "success",
                "output": output,
                "error": ""
            }
        
        except FileNotFoundError as e:
            return {
                "status": "error",
                "output": "",
                "error": f"图片文件不存在: {str(e)}"
            }
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": f"Vision分析失败: {str(e)}"
            }


class VisionTool(BaseTool):
    """图片Vision分析工具 - 调用LLM分析图片内容"""
    
    def execute(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行Vision分析
        
        Parameters:
            image_path (str): 图片文件相对路径（相对于任务目录）
            question (str, optional): 要问的问题，默认"请描述这张图片的内容"
            model (str, optional): 模型名称，默认使用配置中的模型
            save_path (str, optional): 保存分析结果的相对路径
        
        Returns:
            status: "success" 或 "error"
            output: 分析结果文本或保存位置信息
            error: 错误信息（如有）
        """
        try:
            # 获取参数
            image_path = parameters.get("image_path")
            question = parameters.get("question", "请描述这张图片的内容")
            model = parameters.get("model")
            save_path = parameters.get("save_path")
            
            if not image_path:
                return {
                    "status": "error",
                    "output": "",
                    "error": "缺少必需参数: image_path"
                }
            
            # 转换为绝对路径
            abs_image_path = get_abs_path(task_id, image_path)
            
            # 调用LLM客户端
            llm_client = get_llm_client()
            
            try:
                result = llm_client.vision_query(
                    image_path=str(abs_image_path),
                    question=question,
                    model=model
                )
                
                # 保存分析结果
                if save_path:
                    abs_save_path = get_abs_path(task_id, save_path)
                    abs_save_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(abs_save_path, 'w', encoding='utf-8') as f:
                        f.write(result)
                    output = f"结果保存在 {save_path}"
                else:
                    output = result
                
                return {
                    "status": "success",
                    "output": output,
                    "error": ""
                }
                
            except FileNotFoundError as e:
                return {
                    "status": "error",
                    "output": "",
                    "error": f"图片文件不存在: {str(e)}"
                }
            except Exception as e:
                return {
                    "status": "error",
                    "output": "",
                    "error": f"Vision分析失败: {str(e)}"
                }
        
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": f"执行失败: {str(e)}"
            }


class CreateImageTool(BaseTool):
    """图片生成工具 - 根据提示词生成图片（支持参考图）"""
    
    def execute(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行图片生成
        
        Parameters:
            prompt (str): 图片提示词
            image_path (str): 生成图片保存的相对路径（相对于任务目录）
            reference_images (list[str], optional): 参考图片相对路径列表（用于图片编辑/风格迁移）
            model (str, optional): 模型名称
            size (str, optional): 图片尺寸，默认 "1024x1024"
            n (int, optional): 生成图片数量，默认 1
        """
        try:
            # 获取参数
            prompt = parameters.get("prompt")
            image_path = parameters.get("image_path")
            reference_images = parameters.get("reference_images")
            model = parameters.get("model")
            size = parameters.get("size", "1024x1024")
            n = parameters.get("n", 1)
            
            if not prompt or not image_path:
                return {
                    "status": "error",
                    "output": "",
                    "error": "缺少必需参数: prompt 或 image_path"
                }
            
            # 转换为绝对路径
            abs_save_path = get_abs_path(task_id, image_path)
            
            # 处理参考图片路径
            abs_reference_images = None
            if reference_images:
                if isinstance(reference_images, str):
                    reference_images = [reference_images]
                abs_reference_images = [str(get_abs_path(task_id, ref_path)) for ref_path in reference_images]
            
            # 确保父目录存在
            abs_save_path.parent.mkdir(parents=True, exist_ok=True)
            
            # 调用LLM客户端
            llm_client = get_llm_client()
            
            try:
                # 生成图片
                result_data = llm_client.create_image(
                    prompt=prompt,
                    model=model,
                    reference_images=abs_reference_images,
                    size=size,
                    n=n
                )
                
                import requests
                import base64
                
                # 处理返回结果（URL 或 Base64）
                results_to_save = [result_data] if isinstance(result_data, str) else result_data
                
                for idx, result in enumerate(results_to_save):
                    # 确定保存路径
                    if idx == 0:
                        save_path = abs_save_path
                    else:
                        # 多个结果时，添加序号
                        stem = abs_save_path.stem
                        suffix = abs_save_path.suffix
                        save_path = abs_save_path.parent / f"{stem}_{idx}{suffix}"
                    
                    if result.startswith('http'):
                        # 下载图片
                        response = requests.get(result, timeout=30)
                        if response.status_code == 200:
                            with open(save_path, 'wb') as f:
                                f.write(response.content)
                        else:
                            return {
                                "status": "error",
                                "output": "",
                                "error": f"下载生成的图片失败: HTTP {response.status_code}"
                            }
                    else:
                        # Base64 数据
                        # 有可能带 data:image/png;base64, 前缀，需要处理
                        if "," in result:
                            result = result.split(",")[1]
                        
                        image_content = base64.b64decode(result)
                        with open(save_path, 'wb') as f:
                            f.write(image_content)
                
                # 构建输出消息
                if len(results_to_save) == 1:
                    output_msg = f"图片已生成并保存至: {image_path}"
                else:
                    output_msg = f"已生成 {len(results_to_save)} 张图片，保存至: {image_path} 及其变体"
                
                return {
                    "status": "success",
                    "output": output_msg,
                    "error": ""
                }
                
            except Exception as e:
                return {
                    "status": "error",
                    "output": "",
                    "error": f"生成图片失败: {str(e)}"
                }
        
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": f"执行失败: {str(e)}"
            }
