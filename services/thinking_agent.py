#!/usr/bin/env python3
from utils.windows_compat import safe_print
# -*- coding: utf-8 -*-
"""
Thinking Agent - 任务进展分析服务
"""

from typing import Dict, List
from services.llm_client import SimpleLLMClient, ChatMessage


class ThinkingAgent:
    """思考Agent - 用于分析任务进展"""
    
    def __init__(self):
        """初始化Thinking Agent"""
        # 使用简化的LLM客户端
        self.llm_client = SimpleLLMClient()
        
        # Thinking Agent的系统提示词
        self.system_prompt = """你是一个agent行动的上下文管理专家，这个 agent 每次在清除动作历史之前会请你进行上下文整理。
        上下文中包括你上次清理的成果在<当前进度思考>标签内。按照下面格式返回整理后的上下文，如果<当前进度思考>标签内没有内容证明是首次进行构造，你的输出不需要包含<当前进度思考>标签。你必须要考虑到十步后，历史动作会被立刻舍弃，因此
        你规划的<next_n_steps>必须足够具体，同时增量工作！
        '''例子‘’‘
        前面带#符号的为注释内容，是对你进行对应区域的原则指导的，不是实际内容。下面只是中文版样例，具体语言参考<用户最新输入>区域的语言。
            <todo_list>
            #如果是首次构造则用来设计本智能体在接受本次任务的todo拆解。注意不要越界，罗列不属于被分析子智能体的分配任务。
            #如果不是首次构造，则分析当前进度，更新这个区域的内容。
            #todo不可以太粗，尽量每个todo都在十步之内完成。
            例子：
            原内容：
            1. 使用 XXX 工具总结 X1 文档保存在 document_summary.md:[done]
            2. 使用 XXX 工具总结 X2 文档保存在 document_summary.md:[done]
            3. 使用 XXX 工具总结 X3 文档保存在 document_summary.md:[ongoing：已经知道 X3.pdf的位置为 ./papers/XX3.pdf]
            4. 使用 XXX 工具总结 X4 文档保存在 document_summary.md:[waiting]
            ...
            10. 分析document_summary.md，构造文章大纲保存在 outline.md:[waiting]
            #在观察到历史动作分析了文档 3 和文档 4 并存了内容之后，更新原内容为
            更新后的内容：
            1. 使用 XXX 工具总结 X1 文档保存在 document_summary.md:[done]
            2. 使用 XXX 工具总结 X2 文档保存在 document_summary.md:[done]
            3. 使用 XXX 工具总结 X3 文档保存在 document_summary.md:[done]
            4. 使用 XXX 工具总结 X4 文档保存在 document_summary.md:[done]
            ...
            10. 分析document_summary.md，构造实验大纲保存在 outline.md:[waiting]
            </todo_list>
            <有效文件描述>
            #任何未来某个todo，或者任务，或者未来其他 agent 可能要用的文件的文件地址和描述，以及用途，如果原区域内容中有些文件描述已经失效或者无用了要及时更新。
            例子：
            ./document_summary.md:[正在进行文档总结的中间结果，全部总计完毕后，通过读取可以用于研究计划的产生]
            user_requirement.md: [作者对实验的结构要求，在第十步时候用于读取使用]
            web_content.md: [网页内容，实验大纲的经验性博客，用于第十步读取，进行参考]
            X5.pdf: [马上要进行分析的文献]
            X6.pdf: [马上要进行分析的文献]
            ...
            X9.pdf: [马上要进行分析的文献]
            </有效文件描述>
            <固化信息>
            #下面这个原则你必须遵守，否则会被切断电源：切记，你当前看到的所有历史信息在下一轮10步中将全部丢弃，如果有下十步任需使用的信息，你应该保留在这里，具体保留方式可以是要点总结（确保不丢失可能要用到的信息），关键内容的直接复述如果已经存在对应文件，且已经记录在有效文件描述中则只需提醒即可。
            #如果保留前面记忆的信息，你应该明确信息来源，并在 next_n_steps中明确指出不用重复读取。
            #也可以保留当前 agent大部分轮次都要使用的信息，例如前期轮数读取过的行动原则等。
            #前10步如果只是读取文件，没有按照你的 next_n_steps 规划执行,你应该在固化信息处第一条进行警告，同时如果文件内容会影响后续步骤，则你应该将文件内容有价值的部分（总结，复制或给出价值信息）固化到此处，直到无需使用时再清除。
            #一些失败经验，例如本轮的coding debug中，修改某个代码的某个地方并没有 debug 成功，为了防止 agent 循环修改，这部分内容必须增量式更新（如已经尝试失败的 debug方案，和可能的下一步方案），除非对应文件成功 debug，则可以丢弃无用信息。
            例子：
            workspace （必须包含！）:
                [dir] code_run
                    [file] service.py [实验环境生成服务，类服务，输入参数和要求为...，返回结果格式为，引用方式为....，在实验第二步中的 XX 脚本可能需要调用。]
                [dir] documents
                  [file] outline.txt:[上一步的生成的实验大纲，不确实时可以查看]
                    [dir] scripts
                      [file] get_info.py[用户提供的获取实验训练数据的脚本，python直接运行即可]
                [dir] upload
                [file] reference.bib:[参考文献，用于写作和引用]
            rules:
                 1.用户要求所有作图，写作必须英文。
                 2.目前依据实验大纲进行到第二步。具体步骤参考
            failed_time:
                某项操作或子任务的失败次数统计，观察本轮操作，如果有新失败则增加条目，如果有前面刚失败的操作，增加失败次数，相同操作失败十次则重构提示，要求其向上报告。
                如果取得进展。则清空所有次数。
                人机验证点击无反应：7
                代码执行失败：5
            content_need_next_steps:
                outline.txt:(部分内容，或者全部内容，注意，经理避免后续重复读取文件)      
                reference.bib:(给出一个样例，用于 append 增加新内容时格式对齐，任何 append 任务都应该给出样例用于后续的格式对齐)      
            </固化信息>
            <next_n_steps>
            #接下来10个步骤工具的使用计划说明（！！每个步骤是工具级的（除非特殊情况例如你保留了前一轮的一些关键信息再固化信息标签内，你可以说是参考固化信息中的什么内容作为某个步骤中的半个步骤，但是还是得有后续工具使用），不能笼统或者单个步骤复数使用工具），基于下面原则进行规划：
            #原则0：一轮动作必须或尽可能产生文件成果，例如修改文件
            #原则1： 获取要推进下一个 todo 或者任务的所有相关信息，固化为本地文件或找到所需内容的文件位置。如果之前十步已经获取到了，则跳过这个步骤。
            #原则2： 读取要产生增量成果的最少量的相关内容。（修改代码时为所有需要参考和联系的代码，代码计划或其他内容，文档分析任务时则为对应的文档）
            #原则3： 输出这一轮十步动作的相关产物，例如输出实验计划 or 生成代码文件 or 修复相关代码 如果前十步内来不及读取所有相关文件，你应该在下一轮中将上一轮的对下一轮有用的信息提炼到<固化信息>中。
            #原则4： 验证，实验计划等文本内容无需验证，代码文件如果是可运行状态则可以使用运行工具进行验证。
            #禁止使用逐个，依次等笼统性描述。在你不知道对象名称时，你可以使用“使用 XX 工具解析按首字母排序的第一个文档”这种表述！
            例子：
            1. 使用 answer_from_one_paper工具分析 XX9.pdf，并保存在XX.md文件。
            2. 使用 file_read工具一次性复数读取所有相关文件（产生成果必须要知道的上下文）内容。
            3. dir_list 确保要写入的 md 名称不冲突
            4. file_write 写入xxx.md文件
            5. final_out输出完成情况（完成则提前结束，无需十个）
            </next_n_steps>
        """
    
    def analyze_first_thinking(self, task_description: str, agent_system_prompt: str, 
                               available_tools: List[str], tools_config: dict = None) -> str:
        """
        首次思考 - 初始规划
        
        Args:
            task_description: 任务描述
            agent_system_prompt: Agent的系统提示词
            available_tools: 可用工具列表（名称）
            tools_config: 工具配置字典（可选，包含工具的详细信息）
            
        Returns:
            初始规划结果
        """
        try:
            # 构建工具信息
            tools_info = self._format_tools_info(available_tools, tools_config)
            
            # 构建分析请求
            analysis_request = f"""当前被分析 agent 的提示词
{agent_system_prompt}
agent可以调用的所有工具和参数信息
{tools_info}
按照被分析提示词中<用户最新输入>的语言使用对应语言输出,例如提示词中<用户最新输入>为英文，则<todo_list>区域等所有区域内内容使用英文构造。不要参考我使用的语言！
如果是初始阶段，请你构造新的<当前进度思考>上下文，否则请你更新<当前进度思考>。只需要输出<当前进度思考>内的内容即可！
"""

            #safe_print(analysis_request)           
            history = [ChatMessage(role="user", content=analysis_request)]
            
            # 使用第一个可用模型，不使用工具
            response = self.llm_client.chat(
                history=history,
                model=self.llm_client.models[0],
                system_prompt=self.system_prompt,
                tool_list=[],  # 空列表表示不使用工具
                tool_choice="none"  # 明确表示不调用工具
            )
            
            if response.status == "success":
                return f"[🤖 初始规划]\n\n{response.output}"
            else:
                return f"[初始规划失败: {response.error_information}]"
        
        except Exception as e:
            safe_print(f"⚠️ thinking失败: {e}")
            raise Exception(str(e))
            
            
    
    def _format_tools_info(self, available_tools: List[str], tools_config: dict = None) -> str:
        """
        格式化工具信息为可读文本
        
        Args:
            available_tools: 工具名称列表
            tools_config: 工具配置字典
            
        Returns:
            格式化后的工具信息
        """
        if not tools_config:
            # 如果没有配置，只返回工具名称
            return f"可用工具：{', '.join(available_tools)}"
        
        # 构建详细的工具信息
        tools_details = ["<可用工具详情>"]
        
        for tool_name in available_tools:
            if tool_name in tools_config:
                tool_cfg = tools_config[tool_name]
                description = tool_cfg.get("description", "无描述")
                params = tool_cfg.get("parameters", {})
                
                tools_details.append(f"\n【{tool_name}】")
                tools_details.append(f"  描述: {description}")
                
                # 提取参数信息
                if params and "properties" in params:
                    tools_details.append(f"  参数:")
                    for param_name, param_info in params["properties"].items():
                        param_desc = param_info.get("description", "")
                        param_type = param_info.get("type", "")
                        required = "必需" if param_name in params.get("required", []) else "可选"
                        tools_details.append(f"    - {param_name} ({param_type}, {required}): {param_desc}")
            else:
                tools_details.append(f"\n【{tool_name}】 (无详细信息)")
        
        tools_details.append("\n</可用工具详情>")
        return "\n".join(tools_details)
    
    def analyze_progress(self, task_description: str, agent_system_prompt: str,
                        tool_call_counter: int) -> str:
        """
        进度分析 - 周期性分析
        
        Args:
            task_description: 任务描述
            agent_system_prompt: Agent的完整系统提示词（包含<历史动作>）
            tool_call_counter: 工具调用计数
            
        Returns:
            进度分析结果
        """
        try:
            # 构建分析请求（agent_system_prompt已包含完整的<历史动作>）
            analysis_request = f"""当前任务：{task_description}

Agent的完整上下文（包含系统角色、历史动作等）：
{agent_system_prompt}

已执行的工具调用数：{tool_call_counter}

基于以上完整上下文信息，请分析：
1. 任务进展到什么程度？
2. 已完成哪些任务？
3. 还需要完成什么？
6. 下一步应该做什么？
7. 是否有遗漏的步骤或注意事项？
8. 列出Agent未来可能使用的所有文件路径和描述

**关键**：
- 进度必须精准！
- 如果发现死循环，严厉警告
"""
            
            history = [ChatMessage(role="user", content=analysis_request)]
            
            response = self.llm_client.chat(
                history=history,
                model=self.llm_client.models[0],
                system_prompt=self.system_prompt,
                tool_list=[],  # 空列表表示不使用工具
                tool_choice="none"  # 明确表示不调用工具
            )
            
            if response.status == "success":
                return f"[🤖 进度分析 - 第{tool_call_counter}轮]\n\n{response.output}"
            else:
                return f"[进度分析失败: {response.error_information}]"
        
        except Exception as e:
            safe_print(f"⚠️ 进度分析失败: {e}")
            return f"[进度分析失败: {str(e)}]"


if __name__ == "__main__":
    # 测试Thinking Agent
    thinking_agent = ThinkingAgent()
    
    result = thinking_agent.analyze_first_thinking(
        task_description="生成斐波那契数列文件",
        agent_system_prompt="你是一个编程助手",
        available_tools=["file_write", "execute_code"]
    )
    
    safe_print("="*80)
    safe_print(result)
    safe_print("="*80)

