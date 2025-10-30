#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Paper分析工具 - 论文内容分析
"""

from pathlib import Path
from typing import Dict, Any

from .file_tools import BaseTool, get_abs_path


class PaperAnalyzeTool(BaseTool):
    """论文分析工具 - 解析并分析论文内容"""
    
    def execute(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行论文分析
        
        Parameters:
            paper_path (str): 论文文件相对路径（相对于任务目录）
            question (str, optional): 要问的问题，默认"请总结这篇论文的主要内容"
            parse_save_path (str, optional): 解析结果保存路径，可选
        
        Returns:
            status: "success" 或 "error"
            output: 分析结果文本
            error: 错误信息（如有）
        """
        try:
            # 获取参数
            paper_path = parameters.get("paper_path")
            question = parameters.get("question", "请总结这篇论文的主要内容")
            parse_save_path = parameters.get("parse_save_path")
            
            if not paper_path:
                return {
                    "status": "error",
                    "output": "",
                    "error": "缺少必需参数: paper_path"
                }
            
            # 转换为绝对路径
            abs_paper_path = get_abs_path(task_id, paper_path)
            
            if not abs_paper_path.exists():
                return {
                    "status": "error",
                    "output": "",
                    "error": f"论文文件不存在: {paper_path}"
                }
            
            # 步骤1: 解析文档
            from .document_tools import ParseDocumentTool
            parse_tool = ParseDocumentTool()
            
            parse_params = {
                "path": paper_path  # 使用相对路径
            }
            
            if parse_save_path:
                parse_params["save_path"] = parse_save_path
            
            parse_result = parse_tool.execute(task_id, parse_params)
            
            if parse_result["status"] != "success":
                return {
                    "status": "error",
                    "output": "",
                    "error": f"文档解析失败: {parse_result.get('error', '未知错误')}"
                }
            
            # 步骤2: 读取解析结果
            if parse_save_path:
                # 如果保存了解析结果，读取保存的文件
                read_path = parse_save_path
            else:
                # 否则读取返回的内容
                paper_content = parse_result.get("output", "")
                if not paper_content:
                    return {
                        "status": "error",
                        "output": "",
                        "error": "文档解析结果为空"
                    }
                
                # 临时处理：如果输出是"结果保存在 xxx"，则需要读取该文件
                if "结果保存在" in paper_content:
                    # 提取文件路径
                    import re
                    match = re.search(r'结果保存在\s+(\S+)', paper_content)
                    if match:
                        read_path = match.group(1)
                    else:
                        read_path = paper_path.replace(".pdf", ".txt")
                else:
                    # 直接返回内容
                    read_path = None
                    paper_content = paper_content
            
            # 步骤3: 如果有文件路径，读取文件
            if read_path:
                from .file_tools import FileReadTool
                read_tool = FileReadTool()
                read_result = read_tool.execute(task_id, {"path": read_path})
                
                if read_result["status"] != "success":
                    return {
                        "status": "error",
                        "output": "",
                        "error": f"读取解析结果失败: {read_result.get('error', '未知错误')}"
                    }
                
                paper_content = read_result.get("output", "")
            
            # 步骤4: 使用 LLM 分析内容
            import sys
            import os
            parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if parent_dir not in sys.path:
                sys.path.insert(0, parent_dir)
            
            from llm_client_lite import get_llm_client
            
            try:
                # 获取 LLM 客户端
                llm_client = get_llm_client()
                
                # 调用文本分析
                analysis_result = llm_client.text_query(
                    text=paper_content,
                    question=question
                )
                
                return {
                    "status": "success",
                    "output": analysis_result,
                    "error": ""
                }
                    
            except Exception as e:
                return {
                    "status": "error",
                    "output": "",
                    "error": f"LLM分析失败: {str(e)}"
                }
        
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": f"执行失败: {str(e)}"
            }

