import asyncio
import json
from typing import Dict, List, Tuple, Optional
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import AstrBotConfig, logger
from astrbot.core.utils.session_waiter import session_waiter, SessionController


@register(
    "continuous_message",
    "aliveriver",
    "将用户短时间内发送的多条私聊消息合并成一条发送给LLM（仅私聊模式）",
    "1.0.0"
)
class ContinuousMessagePlugin(Star):
    """
    消息防抖动插件（仅私聊模式）
    
    功能：
    1. 拦截用户短时间内发送的多条私聊消息
    2. 在防抖时间结束后，将这些消息合并成一条发送给LLM
    3. 过滤指令消息，不参与合并
    4. 保持人格设定和对话历史
    5. 支持图片识别和传递
    
    安全设计：
    - 强制仅在私聊启用，避免群聊中不同用户的消息被误合并
    """
    
    # 尝试导入 Image 组件类（用于类型检查）
    _ImageComponent = None
    _image_component_import_failed = False
    try:
        from astrbot.api.message import Image as _ImageComponent
    except ImportError:
        # 如果导入失败，使用类名检查作为后备方案
        # 警告：这种方式依赖于类的内部实现细节，如果框架未来版本重命名了 Image 类，此代码将失效
        _image_component_import_failed = True
        logger.warning("[消息防抖动] 无法导入 Image 组件类，将使用类名检查作为后备方案（存在兼容性风险）")
    
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        
        # 从配置读取参数
        self.debounce_time = float(self.config.get('debounce_time', 2.0))
        self.command_prefixes = self.config.get('command_prefixes', ['/'])
        self.enable_plugin = self.config.get('enable', True)
        self.merge_separator = self.config.get('merge_separator', '\n')
        
        # 输出到 logger
        logger.info(f"[消息防抖动] 插件已加载 - 启用: {self.enable_plugin}, 防抖: {self.debounce_time}秒")
    
    def is_command(self, message: str) -> bool:
        """
        检查消息是否是指令
        
        Args:
            message: 消息内容
            
        Returns:
            bool: 如果是指令返回True，否则返回False
        """
        message = message.strip()
        if not message:
            return False
            
        for prefix in self.command_prefixes:
            if message.startswith(prefix):
                return True
        return False
    
    def _extract_response_text(self, response) -> str:
        """
        从LLM响应对象中稳健地提取文本内容。
        
        Args:
            response: LLM响应对象
            
        Returns:
            str: 提取的文本内容，如果无法提取则返回字符串表示
        """
        possible_attrs = ['completion_text', 'result', 'content', 'text', 'message']
        for attr in possible_attrs:
            text = getattr(response, attr, None)
            if text and isinstance(text, str):
                return text
        return str(response)
    
    def _parse_message(self, message_obj) -> Tuple[str, bool, List[str]]:
        """
        从消息对象中解析文本和图片信息。
        
        这是一个统一的辅助方法，用于避免在多个地方重复相同的解析逻辑。
        
        Args:
            message_obj: 消息对象，包含 message 属性（消息组件列表）
            
        Returns:
            Tuple[str, bool, List[str]]: 
                - 文本内容（如果无法提取则返回空字符串）
                - 是否包含图片
                - 图片URL列表
        """
        text = ""
        has_image = False
        image_urls = []
        
        try:
            for component in message_obj.message:
                # 检查是否是文本组件（Plain 或 Text）
                if hasattr(component, '__class__'):
                    comp_class_name = component.__class__.__name__
                    if comp_class_name == 'Plain' or comp_class_name == 'Text':
                        # 提取原始文本
                        if hasattr(component, 'text'):
                            text += component.text
                        elif hasattr(component, 'content'):
                            text += component.content
                        elif hasattr(component, 'data'):
                            text += str(component.data)
                
                # 检查是否是图片组件
                # 优先使用 isinstance 检查（更健壮）
                if self._ImageComponent is not None:
                    is_image = isinstance(component, self._ImageComponent)
                else:
                    # 后备方案：使用类名检查（存在兼容性风险）
                    # 警告：如果框架未来版本重命名了 Image 类，此代码将失效
                    is_image = (hasattr(component, '__class__') 
                               and component.__class__.__name__ == 'Image')
                
                if is_image:
                    has_image = True
                    # 提取图片URL
                    if hasattr(component, 'url'):
                        image_urls.append(component.url)
                    elif hasattr(component, 'file'):
                        image_urls.append(component.file)
        except (AttributeError, TypeError) as e:
            # 捕获具体的异常类型，记录日志以便调试
            logger.warning(f"[消息防抖动] 解析消息组件时出错: {e}")
        except Exception as e:
            # 捕获其他未知异常，记录日志
            logger.warning(f"[消息防抖动] 解析消息组件时出现未知错误: {e}")
        
        return text, has_image, image_urls
    
    def _process_message(self, ev: AstrMessageEvent, buffer: List[str]) -> bool:
        """处理单条消息，返回是否成功处理（不处理图片URL）"""
        # 使用统一的解析方法提取消息内容
        text, has_image, _ = self._parse_message(ev.message_obj)
        
        # 如果无法从组件提取文本，使用 ev.message_str 作为后备
        if not text:
            text = (ev.message_str or "").strip()
        else:
            text = text.strip()
        
        # 如果既没有文本也没有图片，跳过
        if not text and not has_image:
            return False
        
        # 如果有文本，检查是否是指令
        if text and self.is_command(text):
            return False
        
        # 显示处理日志（优先显示文本，如果没有文本则显示图片标识）
        display_msg = text[:50] if text else "[图片]"
        logger.info(f"[消息防抖动] 处理消息: {display_msg}")
        
        # 普通消息或图片：接管处理，阻止后续默认流程
        ev.stop_event()
        
        # 如果有文本，加入缓冲区
        if text:
            buffer.append(text)
        
        # 如果只有图片没有文本，添加占位符
        if has_image and not text:
            buffer.append("[图片]")
        
        return True
    
    async def _send_to_llm(self, merged_msg: str, img_urls: List[str], unified_msg_origin: str):
        """将合并的消息发送给 LLM 并返回响应"""
        if not merged_msg:
            return None
        
        # 获取 LLM 提供商
        provider = self.context.get_using_provider(umo=unified_msg_origin)
        if not provider:
            logger.warning(f"[消息防抖动] 未找到 LLM 提供商")
            return None
        
        # 获取人格设定
        try:
            persona = await self.context.persona_manager.get_default_persona_v3(umo=unified_msg_origin)
            
            if persona:
                if isinstance(persona, dict):
                    system_prompt = persona.get('prompt') or persona.get('system_prompt')
                else:
                    system_prompt = getattr(persona, 'prompt', None) or getattr(persona, 'system_prompt', None)
            else:
                system_prompt = None
                
        except Exception as e:
            logger.error(f"[消息防抖动] 获取会话人格失败: {e}")
            system_prompt = None
        
        # 获取对话历史
        context_history = []
        try:
            conv_mgr = self.context.conversation_manager
            curr_cid = await conv_mgr.get_curr_conversation_id(unified_msg_origin)
            conversation = await conv_mgr.get_conversation(
                unified_msg_origin,
                curr_cid,
                create_if_not_exists=True
            )
            
            if conversation and conversation.history:
                try:
                    context_history = json.loads(conversation.history)
                except json.JSONDecodeError as e:
                    logger.warning(f"[消息防抖动] 对话历史格式错误，无法解析: {e}")
                    context_history = []
                except TypeError as e:
                    # conversation.history 可能为 None 或其他非字符串类型
                    logger.warning(f"[消息防抖动] 对话历史类型错误: {e}")
                    context_history = []
        except Exception as e:
            logger.warning(f"[消息防抖动] 获取对话历史失败: {e}")
            context_history = []
        
        # 调用 LLM
        try:
            response = await provider.text_chat(
                prompt=merged_msg,
                context=context_history,
                system_prompt=system_prompt,
                image_urls=img_urls if img_urls else None
            )
            
            # 获取响应文本
            response_text = self._extract_response_text(response)
            
            if not response_text:
                logger.error(f"[消息防抖动] LLM 响应为空")
                return None
            
            # 更新对话历史
            try:
                context_history.append({"role": "user", "content": merged_msg})
                context_history.append({"role": "assistant", "content": response_text})
                await conv_mgr.update_conversation(
                    unified_msg_origin,
                    curr_cid,
                    history=context_history
                )
            except Exception as e:
                logger.warning(f"[消息防抖动] 更新对话历史失败: {e}")
            
            return response_text
            
        except Exception as e:
            logger.error(f"[消息防抖动] LLM 请求失败: {e}", exc_info=True)
            return None
    
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=100)
    async def handle_private_msg(self, event: AstrMessageEvent):
        """
        私聊消息防抖逻辑（仅私聊可用，避免群聊越权问题）
        
        - 如果是指令消息：直接放行，不干预
        - 否则，参与防抖聚合：
          - debounce_time 秒内的新消息会被合并
          - 期间若出现指令，则结束本轮聚合
          - 超时后，把本轮聚合的文本一次性交给 LLM
        """
        # 如果插件未启用，直接返回
        if not self.enable_plugin:
            return
        
        # 从原始消息组件中提取完整文本（包含指令前缀）
        # 因为 event.message_str 可能已经去掉了前缀
        raw_text, has_image, _ = self._parse_message(event.message_obj)
        
        # 如果无法从组件提取文本，使用 event.message_str 作为后备
        if not raw_text:
            raw_text = (event.message_str or "").strip()
        else:
            raw_text = raw_text.strip()
        
        # 如果消息为空且没有图片，直接返回
        if not raw_text and not has_image:
            return
        
        # 检查是否是指令（使用原始文本，包含前缀）
        if raw_text and self.is_command(raw_text):
            return
        
        # 显示开始防抖处理的日志（优先显示文本，如果没有文本则显示图片标识）
        display_msg = raw_text[:50] if raw_text else "[图片]"
        logger.info(f"[消息防抖动] 开始防抖处理: {display_msg}")
        
        # 防抖时间 <= 0，不进行防抖
        if self.debounce_time <= 0:
            return
        
        # 消息缓冲区
        buffer: List[str] = []
        
        # 存储图片 URL 列表
        image_urls = []
        
        # 先处理第一条消息
        raw_text, has_image, parsed_image_urls = self._parse_message(event.message_obj)
        if not raw_text:
            raw_text = (event.message_str or "").strip()
        else:
            raw_text = raw_text.strip()
        
        if not raw_text and not has_image:
            return
        
        if raw_text and self.is_command(raw_text):
            return
        
        image_urls.extend(parsed_image_urls)
        success = self._process_message(event, buffer)
        if not success:
            return
        
        # 会话控制器：收集后续消息 + 超时判断
        @session_waiter(timeout=self.debounce_time, record_history_chains=False)
        async def collect_messages(
            controller: SessionController,
            ev: AstrMessageEvent,
        ):
            nonlocal buffer, image_urls
            
            # 使用统一的解析方法提取消息内容
            text, has_image, parsed_image_urls = self._parse_message(ev.message_obj)
            
            # 如果无法从组件提取文本，使用 ev.message_str 作为后备
            if not text:
                text = (ev.message_str or "").strip()
            else:
                text = text.strip()
            
            # 防止 session_waiter 重复处理第一条消息
            # 说明：session_waiter 可能会在第一次调用时再次处理初始事件
            # 如果 buffer 中只有一条消息，且当前消息内容与第一条相同，则跳过
            # 这样可以避免同一条消息被处理两次
            if len(buffer) == 1 and text == buffer[0]:
                logger.info(f"[消息防抖动] 跳过重复处理的第一条消息: {text[:50]}")
                # 重置超时时间，继续等待后续消息
                controller.keep(timeout=self.debounce_time, reset_timeout=True)
                return
            
            # 添加图片 URL
            image_urls.extend(parsed_image_urls)
            
            # 处理消息
            success = self._process_message(ev, buffer)
            if success:
                # 重置超时时间
                controller.keep(timeout=self.debounce_time, reset_timeout=True)
            else:
                # 如果是指令，停止会话
                if text and self.is_command(text):
                    controller.stop()
                return
        
        try:
            # 启动会话控制器，等待后续消息
            await collect_messages(event)
            # 如果正常返回（没有超时），说明被 controller.stop() 停止了（可能是指令中断）
            logger.info(f"[消息防抖动] 防抖会话被停止（可能是指令中断）")
            
            # 如果有已收集的消息，先提交给 LLM
            if buffer:
                merged_message = self.merge_separator.join(buffer).strip()
                if merged_message:
                    logger.info(f"[消息防抖动] 指令中断，提交已收集的 {len(buffer)} 条消息给 LLM")
                    unified_msg_origin = event.unified_msg_origin
                    response_text = await self._send_to_llm(merged_message, image_urls, unified_msg_origin)
                    if response_text:
                        yield event.plain_result(response_text)
            
            # 让指令正常执行（不阻止事件传播）
            return
            
        except TimeoutError:
            # 超时：合并并发送给 LLM
            merged_message = self.merge_separator.join(buffer).strip()
            if not merged_message:
                return

            logger.info(f"[消息防抖动] 防抖超时，合并了 {len(buffer)} 条消息，图片数: {len(image_urls)}")
            
            # 接管这轮消息
            event.stop_event()
            
            # 调用 LLM
            unified_msg_origin = event.unified_msg_origin
            response_text = await self._send_to_llm(merged_message, image_urls, unified_msg_origin)
            
            if response_text:
                yield event.plain_result(response_text)
            else:
                yield event.plain_result("抱歉，AI 没有返回有效响应。")
        
        except Exception as e:
            logger.error(f"[消息防抖动] 插件内部错误: {e}", exc_info=True)
            event.stop_event()
            yield event.plain_result(f"插件内部错误: {str(e)}")
