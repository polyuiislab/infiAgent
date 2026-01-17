#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vision分析工具 - 图片内容分析
"""

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
