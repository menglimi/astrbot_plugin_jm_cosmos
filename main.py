"""
JM-Cosmos II - AstrBot JM漫画下载插件

支持搜索、下载禁漫天堂的漫画本子，基于jmcomic库
"""

import json
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register

from .core import (
    DownloadQuotaManager,
    JMAuthManager,
    JMBrowser,
    JMConfigManager,
    JMDownloadManager,
    JMPacker,
)
from .utils import MessageFormatter, generate_album_filename, send_with_recall

# 插件名称常量
PLUGIN_NAME = "jm_cosmos2"


@register(
    "jm_cosmos2",
    "GEMILUXVII",
    "JM漫画下载插件 - 支持搜索、下载禁漫天堂的漫画本子，支持加密PDF/ZIP打包",
    "2.6.6",
    "https://github.com/GEMILUXVII/astrbot_plugin_jm_cosmos",
)
class JMCosmosPlugin(Star):
    """AstrBot JM漫画下载插件"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config

        logger.info("正在初始化 JM-Cosmos II 插件...")

        # 获取数据目录
        try:
            self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
            self.data_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"JM-Cosmos II 数据目录: {self.data_dir}")
        except Exception as e:
            logger.error(f"获取数据目录失败: {e}")
            self.data_dir = Path(__file__).parent / "data"
            self.data_dir.mkdir(parents=True, exist_ok=True)

        # 初始化配置管理器
        self.config_manager = JMConfigManager(config, self.data_dir)

        # 初始化下载管理器
        self.download_manager = JMDownloadManager(self.config_manager)

        # 初始化浏览查询器
        self.browser = JMBrowser(self.config_manager)

        # 初始化认证管理器
        self.auth_manager = JMAuthManager(self.config_manager)

        # 初始化下载配额管理器
        self.quota_manager = DownloadQuotaManager(self.data_dir / "quota.db")

        # 调试模式
        self.debug_mode = self.config_manager.debug_mode
        if self.debug_mode:
            logger.warning("JM-Cosmos II 调试模式已启用")

        logger.info("JM-Cosmos II 插件初始化完成")

    def _check_permission(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """
        检查用户权限

        Returns:
            (是否有权限, 错误消息)
        """
        user_id = event.get_sender_id()
        group_id = event.get_group_id()

        # 检查管理员权限
        if not self.config_manager.is_admin(user_id):
            return False, MessageFormatter.format_error("permission")

        # 检查群启用状态
        if group_id and not self.config_manager.is_group_enabled(group_id):
            return False, MessageFormatter.format_error("group_disabled")

        return True, ""

    def _check_skill_permission(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """检查 LLM 工具是否允许在当前会话使用"""
        if not self.config_manager.jm_skill_enabled:
            return False, "JM skill 已在配置中关闭"
        return self._check_permission(event)

    def _tool_json(self, payload: dict) -> str:
        """返回 LLM 友好的 JSON 文本"""
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _normalize_tool_limit(self, limit: int | str | None = None) -> int:
        """规范化 LLM 工具返回数量"""
        try:
            value = int(limit) if limit is not None else self.config_manager.search_page_size
        except (TypeError, ValueError):
            value = self.config_manager.search_page_size
        return max(1, min(self.config_manager.jm_skill_max_results, value))

    def _normalize_search_keyword(self, keyword: str) -> str:
        """规范化 LLM 传入的搜索关键词，优先保留 JM 标签搜索语法"""
        keyword = str(keyword or "").strip()
        if not keyword:
            return ""

        # LLM 偶尔会用中文/英文逗号列多个标签；JM 标签并集搜索使用空格分隔。
        for separator in ("，", ",", "、"):
            keyword = keyword.replace(separator, " ")

        parts = keyword.split()
        if len(parts) <= 2:
            return " ".join(parts)

        # 标签搜索约定最多两个标签；保留第一个标签和第一个带 +/- 的约束标签。
        constrained = next((part for part in parts[1:] if part[:1] in {"+", "-"}), None)
        if constrained:
            return f"{parts[0]} {constrained}"
        return " ".join(parts[:2])

    async def _send_message_chain(self, event: AstrMessageEvent, chain: MessageChain):
        """向当前会话发送消息链"""
        await self.context.send_message(event.unified_msg_origin, chain)

    async def _download_album_for_skill(
        self, event: AstrMessageEvent, album_id: str
    ) -> dict:
        """供 LLM tool 使用的本子下载流程"""
        user_id = event.get_sender_id()
        limit = self.config_manager.daily_download_limit
        is_admin = str(user_id) in self.config_manager.admin_list

        if limit > 0 and not is_admin:
            can_download, used, total = self.quota_manager.check_quota(user_id, limit)
            if not can_download:
                return {
                    "success": False,
                    "message": f"今日下载次数已达上限 ({used}/{total})",
                }

        await self._send_message_chain(
            event, MessageChain([Comp.Plain(f"⏳ 开始下载本子 {album_id}，请稍候...")])
        )

        result = await self.download_manager.download_album(album_id)
        if not result.success:
            return {
                "success": False,
                "message": MessageFormatter.format_error(
                    "download_failed", result.error_message
                ),
            }

        output_name = generate_album_filename(
            album_id=album_id,
            password=self.config_manager.pack_password,
            show_password=self.config_manager.filename_show_password,
        )

        packer = JMPacker(
            pack_format=self.config_manager.pack_format,
            password=self.config_manager.pack_password,
        )
        pack_result = packer.pack(
            source_dir=result.save_path,
            output_name=output_name,
        )

        if limit > 0:
            self.quota_manager.consume_quota(user_id)

        result_msg = MessageFormatter.format_download_result(result, pack_result)

        sent_file = False
        if (
            pack_result.success
            and pack_result.output_path
            and pack_result.format != "none"
        ):
            file_chain = MessageChain(
                [
                    Comp.Plain(result_msg),
                    Comp.File(
                        name=pack_result.output_path.name,
                        file=str(pack_result.output_path),
                    ),
                ]
            )

            if self.config_manager.auto_recall_enabled:
                await send_with_recall(
                    event,
                    file_chain,
                    self.config_manager.auto_recall_delay,
                )
            else:
                await self._send_message_chain(event, file_chain)

            sent_file = True

            if self.config_manager.auto_delete_after_send:
                JMPacker.cleanup(result.save_path)
                JMPacker.cleanup(pack_result.output_path)
        else:
            await self._send_message_chain(event, MessageChain([Comp.Plain(result_msg)]))

        return {
            "success": True,
            "message": "下载完成，已发送到当前会话" if sent_file else "下载完成",
            "album_id": album_id,
            "title": result.title,
            "pack_format": pack_result.format if pack_result else None,
            "sent_file": sent_file,
        }

    @filter.command("jmhelp")
    async def help_command(self, event: AstrMessageEvent):
        """显示帮助信息"""
        yield event.plain_result(MessageFormatter.format_help())

    @filter.command("jm")
    async def download_album_command(
        self, event: AstrMessageEvent, album_id: str = None
    ):
        """
        下载指定ID的漫画本子

        用法: /jm <ID>
        示例: /jm 123456
        """
        # 权限检查
        has_perm, error_msg = self._check_permission(event)
        if not has_perm:
            yield event.plain_result(error_msg)
            return

        # 参数检查
        if album_id is None:
            yield event.plain_result(
                "❌ 请提供本子ID\n用法: /jm <ID>\n示例: /jm 123456"
            )
            return

        # 转换为字符串并验证ID格式
        album_id = str(album_id).strip()
        if not album_id.isdigit():
            yield event.plain_result(MessageFormatter.format_error("invalid_id"))
            return

        # 检查下载配额（管理员豁免）
        user_id = event.get_sender_id()
        limit = self.config_manager.daily_download_limit
        is_admin = str(user_id) in self.config_manager.admin_list
        if limit > 0 and not is_admin:
            can_download, used, total = self.quota_manager.check_quota(user_id, limit)
            if not can_download:
                yield event.plain_result(
                    f"❌ 今日下载次数已达上限 ({used}/{total})\n请明天再试"
                )
                return

        try:
            # 发送开始下载提示
            yield event.plain_result(f"⏳ 开始下载本子 {album_id}，请稍候...")

            # 如果配置了发送封面预览，获取详情和封面
            if self.config_manager.send_cover_preview:
                detail = await self.browser.get_album_detail(album_id)
                if detail:
                    # 获取封面图片
                    cover_dir = self.config_manager.download_dir / "covers"
                    cover_path = await self.browser.get_album_cover(album_id, cover_dir)

                    if cover_path and cover_path.exists():
                        # 构建封面消息链
                        from astrbot.api.event import MessageChain

                        cover_chain = MessageChain(
                            [
                                Comp.Image(file=str(cover_path)),
                                Comp.Plain(MessageFormatter.format_album_info(detail)),
                            ]
                        )

                        # 根据配置决定是否对封面消息自动撤回
                        if self.config_manager.cover_recall_enabled:
                            await send_with_recall(
                                event,
                                cover_chain,
                                self.config_manager.auto_recall_delay,
                            )
                        else:
                            yield event.chain_result(cover_chain.chain)
                    else:
                        yield event.plain_result(
                            MessageFormatter.format_album_info(detail)
                        )

            # 执行下载
            result = await self.download_manager.download_album(album_id)

            if not result.success:
                yield event.plain_result(
                    MessageFormatter.format_error(
                        "download_failed", result.error_message
                    )
                )
                return

            # 生成文件名
            output_name = generate_album_filename(
                album_id=album_id,
                password=self.config_manager.pack_password,
                show_password=self.config_manager.filename_show_password,
            )

            # 打包文件
            packer = JMPacker(
                pack_format=self.config_manager.pack_format,
                password=self.config_manager.pack_password,
            )

            pack_result = packer.pack(
                source_dir=result.save_path,
                output_name=output_name,
            )

            # 发送结果消息
            # 下载成功，消耗配额
            if limit > 0:
                self.quota_manager.consume_quota(user_id)

            result_msg = MessageFormatter.format_download_result(result, pack_result)

            if (
                pack_result.success
                and pack_result.output_path
                and pack_result.format != "none"
            ):
                # 构建文件路径 - 调试输出
                file_path_str = str(pack_result.output_path)
                logger.info(f"准备发送文件: {file_path_str}")

                # 构建消息链
                from astrbot.api.event import MessageChain

                file_chain = MessageChain(
                    [
                        Comp.Plain(result_msg),
                        Comp.File(
                            name=pack_result.output_path.name,
                            file=file_path_str,
                        ),
                    ]
                )

                # 根据配置决定是否使用自动撤回
                if self.config_manager.auto_recall_enabled:
                    await send_with_recall(
                        event,
                        file_chain,
                        self.config_manager.auto_recall_delay,
                    )
                else:
                    yield event.chain_result(file_chain.chain)

                # 自动清理
                if self.config_manager.auto_delete_after_send:
                    JMPacker.cleanup(result.save_path)
                    JMPacker.cleanup(pack_result.output_path)
            else:
                yield event.plain_result(result_msg)

        except Exception as e:
            logger.error(f"下载本子失败: {e}")
            if self.debug_mode:
                import traceback

                logger.error(traceback.format_exc())
            yield event.plain_result(
                MessageFormatter.format_error("download_failed", str(e))
            )

    @filter.command("jmc")
    async def download_photo_command(
        self, event: AstrMessageEvent, album_id: str = None, chapter_index: str = None
    ):
        """
        下载指定本子的指定章节

        用法: /jmc <本子ID> <章节序号>
        示例: /jmc 123456 3
        """
        # 权限检查
        has_perm, error_msg = self._check_permission(event)
        if not has_perm:
            yield event.plain_result(error_msg)
            return

        # 参数检查
        if album_id is None or chapter_index is None:
            yield event.plain_result(
                "❌ 请提供本子ID和章节序号\n用法: /jmc <本子ID> <章节序号>\n示例: /jmc 123456 3"
            )
            return

        album_id = str(album_id).strip()
        if not album_id.isdigit():
            yield event.plain_result(MessageFormatter.format_error("invalid_id"))
            return

        # 验证章节序号
        try:
            chapter_idx = int(chapter_index)
            if chapter_idx < 1:
                yield event.plain_result("❌ 章节序号必须大于0")
                return
        except ValueError:
            yield event.plain_result("❌ 章节序号必须是数字")
            return

        # 检查下载配额（管理员豁免）
        user_id = event.get_sender_id()
        limit = self.config_manager.daily_download_limit
        is_admin = str(user_id) in self.config_manager.admin_list
        if limit > 0 and not is_admin:
            can_download, used, total = self.quota_manager.check_quota(user_id, limit)
            if not can_download:
                yield event.plain_result(
                    f"❌ 今日下载次数已达上限 ({used}/{total})\n请明天再试"
                )
                return

        try:
            yield event.plain_result(
                f"⏳ 正在获取本子 {album_id} 的第 {chapter_idx} 章节信息..."
            )

            # 获取章节的真正 photo_id
            chapter_info = await self.browser.get_photo_id_by_index(
                album_id, chapter_idx
            )

            if chapter_info is None:
                yield event.plain_result(
                    f"❌ 无法获取章节信息\n可能的原因:\n"
                    f"• 本子 {album_id} 不存在\n"
                    f"• 第 {chapter_idx} 章节不存在"
                )
                return

            photo_id, photo_title, total_chapters = chapter_info

            yield event.plain_result(
                f"📖 找到章节: {photo_title}\n"
                f"📚 章节: {chapter_idx}/{total_chapters}\n"
                f"⏳ 开始下载..."
            )

            # 使用真正的 photo_id 下载
            result = await self.download_manager.download_photo(photo_id)

            if not result.success:
                yield event.plain_result(
                    MessageFormatter.format_error(
                        "download_failed", result.error_message
                    )
                )
                return

            # 生成文件名（带章节号）
            output_name = generate_album_filename(
                album_id=album_id,
                password=self.config_manager.pack_password,
                chapter_idx=chapter_idx,
                show_password=self.config_manager.filename_show_password,
            )

            # 打包
            packer = JMPacker(
                pack_format=self.config_manager.pack_format,
                password=self.config_manager.pack_password,
            )

            pack_result = packer.pack(
                source_dir=result.save_path,
                output_name=output_name,
            )

            # 下载成功，消耗配额
            if limit > 0:
                self.quota_manager.consume_quota(user_id)

            result_msg = MessageFormatter.format_download_result(result, pack_result)

            if (
                pack_result.success
                and pack_result.output_path
                and pack_result.format != "none"
            ):
                file_path_str = str(pack_result.output_path)
                logger.info(f"准备发送章节文件: {file_path_str}")

                # 构建消息链
                from astrbot.api.event import MessageChain

                file_chain = MessageChain(
                    [
                        Comp.Plain(result_msg),
                        Comp.File(
                            name=pack_result.output_path.name,
                            file=file_path_str,
                        ),
                    ]
                )

                # 根据配置决定是否使用自动撤回
                if self.config_manager.auto_recall_enabled:
                    await send_with_recall(
                        event,
                        file_chain,
                        self.config_manager.auto_recall_delay,
                    )
                else:
                    yield event.chain_result(file_chain.chain)

                if self.config_manager.auto_delete_after_send:
                    JMPacker.cleanup(result.save_path)
                    JMPacker.cleanup(pack_result.output_path)
            else:
                yield event.plain_result(result_msg)

        except Exception as e:
            logger.error(f"下载章节失败: {e}")
            yield event.plain_result(
                MessageFormatter.format_error("download_failed", str(e))
            )

    @filter.command("jms")
    async def search_command(
        self, event: AstrMessageEvent, keyword: str = None, page: int = 1
    ):
        """
        搜索漫画

        用法: /jms <关键词> [页码]
        示例: /jms 标签名
        示例: /jms 标签名 2
        """
        # 权限检查
        has_perm, error_msg = self._check_permission(event)
        if not has_perm:
            yield event.plain_result(error_msg)
            return

        if keyword is None:
            yield event.plain_result(
                "❌ 请提供搜索关键词\n用法: /jms <关键词> [页码]\n示例: /jms 标签名\n示例: /jms 标签名 2"
            )
            return

        keyword = str(keyword).strip()
        if not keyword:
            yield event.plain_result("❌ 搜索关键词不能为空")
            return

        # 验证页码
        try:
            page = int(page)
            if page < 1:
                page = 1
        except (ValueError, TypeError):
            page = 1

        try:
            yield event.plain_result(f"🔍 正在搜索: {keyword} (第{page}页)...")

            results = await self.browser.search_albums(keyword, page)

            # 限制结果数量
            page_size = self.config_manager.search_page_size
            results = results[:page_size]

            result_msg = MessageFormatter.format_search_results(results, keyword, page)
            yield event.plain_result(result_msg)

        except Exception as e:
            logger.error(f"搜索失败: {e}")
            yield event.plain_result(MessageFormatter.format_error("network", str(e)))

    @filter.llm_tool(name="jm_search_albums")
    async def jm_search_albums_tool(
        self,
        event: AstrMessageEvent,
        keyword: str,
        page: int = 1,
        limit: int = None,
    ) -> str:
        """搜索 JM 本子，优先使用标签进行搜索。

        当用户自然语言表达想找、推荐、检索某类本子/漫画/标签/作者/题材时，模型可以自主调用本工具。
        尽可能把用户需求转成标签关键词再搜索。
        标签最多不超过两个。多标签格式:
        - "标签1 +标签2": 交集，同时包含两个标签
        - "标签1 -标签2": 排除，包含标签1但不包含标签2
        - "标签1 标签2": 并集，包含任一标签
        本工具只返回搜索结果和可用于后续查看/下载的 ID，不会自动下载或发送文件。

        Args:
            keyword(string): 搜索关键词，优先传标签表达式；也可以是作者、标题或题材。
            page(number): 搜索页码，默认 1。
            limit(number): 返回数量，默认使用插件搜索页大小，最大受配置限制。
        """
        has_perm, error_msg = self._check_skill_permission(event)
        if not has_perm:
            return self._tool_json({"success": False, "message": error_msg})

        keyword = self._normalize_search_keyword(keyword)
        if not keyword:
            return self._tool_json({"success": False, "message": "缺少关键词 keyword"})

        try:
            page = max(1, int(page))
        except (TypeError, ValueError):
            page = 1
        limit = self._normalize_tool_limit(limit)

        try:
            results = await self.browser.search_albums(keyword, page)
            results = results[:limit]
            return self._tool_json(
                {
                    "success": True,
                    "keyword": keyword,
                    "page": page,
                    "count": len(results),
                    "results": results,
                    "usage_hint": "可用 /jmi <ID> 查看详情，或 /jm <ID> 下载。",
                }
            )
        except Exception as e:
            logger.error(f"LLM 搜索本子失败: {e}")
            return self._tool_json({"success": False, "message": str(e)})

    @filter.llm_tool(name="jm_get_album_detail")
    async def jm_get_album_detail_tool(
        self, event: AstrMessageEvent, album_id: str
    ) -> str:
        """获取 JM 本子详情。

        当模型已经从搜索、排行、推荐或用户输入中得到本子 ID，并需要确认标题、标签、作者、章节数等信息时调用。
        本工具不会下载本子。

        Args:
            album_id(string): 本子 ID，纯数字。
        """
        has_perm, error_msg = self._check_skill_permission(event)
        if not has_perm:
            return self._tool_json({"success": False, "message": error_msg})

        album_id = str(album_id or "").strip()
        if not album_id.isdigit():
            return self._tool_json({"success": False, "message": "album_id 必须是纯数字"})

        try:
            detail = await self.browser.get_album_detail(album_id)
            if not detail:
                return self._tool_json({"success": False, "message": "未找到本子"})
            return self._tool_json(
                {
                    "success": True,
                    "album": detail,
                    "usage_hint": "用户明确要下载时，可提示使用 /jm <ID>。",
                }
            )
        except Exception as e:
            logger.error(f"LLM 获取本子详情失败: {e}")
            return self._tool_json({"success": False, "message": str(e)})

    @filter.llm_tool(name="jm_download_album")
    async def jm_download_album_tool(
        self, event: AstrMessageEvent, album_id: str
    ) -> str:
        """下载并发送指定 JM 本子。

        仅当用户明确表达“下载这个本子/发这个本子/下 ID 为 xxx 的本子”时调用本工具。
        不要通过 execute_command 执行 "/jm <ID>"；需要下载时直接调用本工具。
        如果用户只是想搜索、看看详情、找推荐，不要调用本工具。

        Args:
            album_id(string): 要下载的本子 ID，纯数字。
        """
        has_perm, error_msg = self._check_skill_permission(event)
        if not has_perm:
            return self._tool_json({"success": False, "message": error_msg})

        album_id = str(album_id or "").strip()
        if not album_id.isdigit():
            return self._tool_json({"success": False, "message": "album_id 必须是纯数字"})

        try:
            return self._tool_json(await self._download_album_for_skill(event, album_id))
        except Exception as e:
            logger.error(f"LLM 下载本子失败: {e}")
            if self.debug_mode:
                import traceback

                logger.error(traceback.format_exc())
            return self._tool_json({"success": False, "message": str(e)})

    @filter.llm_tool(name="jm_recommend_albums")
    async def jm_recommend_albums_tool(
        self,
        event: AstrMessageEvent,
        category: str = "all",
        order_by: str = "hot",
        time_range: str = "week",
        page: int = 1,
        limit: int = None,
    ) -> str:
        """按分类、排序和时间获取 JM 推荐/浏览结果。

        当用户没有给出明确关键词，只是想让 bot 自主找一些热门、最新、某分类本子时调用。
        可选分类: all, doujin, single, short, hanman, meiman, 3d, cosplay, another。
        可选排序: hot, new, pic, like。
        可选时间: day, week, month, all。

        Args:
            category(string): 分类，默认 all。
            order_by(string): 排序，默认 hot。
            time_range(string): 时间范围，默认 week。
            page(number): 页码，默认 1。
            limit(number): 返回数量，默认使用插件搜索页大小，最大受配置限制。
        """
        has_perm, error_msg = self._check_skill_permission(event)
        if not has_perm:
            return self._tool_json({"success": False, "message": error_msg})

        from .core.browser import JMBrowser

        category = str(category or "all").lower().strip()
        order_by = str(order_by or "hot").lower().strip()
        time_range = str(time_range or "week").lower().strip()

        if category not in JMBrowser.get_category_list():
            category = "all"
        if order_by not in JMBrowser.get_order_list():
            order_by = "hot"
        if time_range not in JMBrowser.get_time_list():
            time_range = "week"

        try:
            page = max(1, int(page))
        except (TypeError, ValueError):
            page = 1
        limit = self._normalize_tool_limit(limit)

        try:
            results = await self.browser.get_category_albums(
                category=category,
                order_by=order_by,
                time_range=time_range,
                page=page,
            )
            results = results[:limit]
            return self._tool_json(
                {
                    "success": True,
                    "category": category,
                    "order_by": order_by,
                    "time_range": time_range,
                    "page": page,
                    "count": len(results),
                    "results": results,
                    "usage_hint": "可用 jm_get_album_detail 进一步查看某个 ID。",
                }
            )
        except Exception as e:
            logger.error(f"LLM 获取推荐本子失败: {e}")
            return self._tool_json({"success": False, "message": str(e)})

    @filter.llm_tool(name="jm_ranking_albums")
    async def jm_ranking_albums_tool(
        self,
        event: AstrMessageEvent,
        ranking_type: str = "week",
        page: int = 1,
        limit: int = None,
    ) -> str:
        """获取 JM 日/周/月排行榜。

        当用户想看热门、排行榜、最近大家在看的本子时调用。
        可选 ranking_type: day, week, month。

        Args:
            ranking_type(string): 排行榜类型，day/week/month，默认 week。
            page(number): 页码，默认 1。
            limit(number): 返回数量，默认使用插件搜索页大小，最大受配置限制。
        """
        has_perm, error_msg = self._check_skill_permission(event)
        if not has_perm:
            return self._tool_json({"success": False, "message": error_msg})

        ranking_type = str(ranking_type or "week").lower().strip()
        if ranking_type not in ("day", "week", "month"):
            ranking_type = "week"
        try:
            page = max(1, int(page))
        except (TypeError, ValueError):
            page = 1
        limit = self._normalize_tool_limit(limit)

        try:
            if ranking_type == "day":
                results = await self.browser.get_day_ranking(page)
            elif ranking_type == "month":
                results = await self.browser.get_month_ranking(page)
            else:
                results = await self.browser.get_week_ranking(page)

            results = results[:limit]
            return self._tool_json(
                {
                    "success": True,
                    "ranking_type": ranking_type,
                    "page": page,
                    "count": len(results),
                    "results": results,
                    "usage_hint": "可用 jm_get_album_detail 进一步查看某个 ID。",
                }
            )
        except Exception as e:
            logger.error(f"LLM 获取排行榜失败: {e}")
            return self._tool_json({"success": False, "message": str(e)})

    @filter.command("jmi")
    async def info_command(self, event: AstrMessageEvent, album_id: str = None):
        """
        查看本子详情

        用法: /jmi <ID>
        示例: /jmi 123456
        """
        # 权限检查
        has_perm, error_msg = self._check_permission(event)
        if not has_perm:
            yield event.plain_result(error_msg)
            return

        if album_id is None:
            yield event.plain_result(
                "❌ 请提供本子ID\n用法: /jmi <ID>\n示例: /jmi 123456"
            )
            return

        album_id = str(album_id).strip()
        if not album_id.isdigit():
            yield event.plain_result(MessageFormatter.format_error("invalid_id"))
            return

        try:
            yield event.plain_result(f"📖 正在获取本子 {album_id} 的详情...")

            detail = await self.browser.get_album_detail(album_id)

            if not detail:
                yield event.plain_result(MessageFormatter.format_error("not_found"))
                return

            # 根据配置决定是否发送封面图片
            if self.config_manager.send_cover_preview:
                cover_dir = self.config_manager.download_dir / "covers"
                cover_path = await self.browser.get_album_cover(album_id, cover_dir)

                if cover_path and cover_path.exists():
                    # 构建封面消息链
                    from astrbot.api.event import MessageChain

                    cover_chain = MessageChain(
                        [
                            Comp.Image(file=str(cover_path)),
                            Comp.Plain(MessageFormatter.format_album_info(detail)),
                        ]
                    )

                    # 根据配置决定是否对封面消息自动撤回
                    if self.config_manager.cover_recall_enabled:
                        await send_with_recall(
                            event,
                            cover_chain,
                            self.config_manager.auto_recall_delay,
                        )
                    else:
                        yield event.chain_result(cover_chain.chain)
                else:
                    yield event.plain_result(MessageFormatter.format_album_info(detail))
            else:
                yield event.plain_result(MessageFormatter.format_album_info(detail))

        except Exception as e:
            logger.error(f"获取详情失败: {e}")
            yield event.plain_result(MessageFormatter.format_error("network", str(e)))

    @filter.command("jmrank")
    async def ranking_command(
        self, event: AstrMessageEvent, ranking_type: str = "week", page: int = 1
    ):
        """
        查看排行榜

        用法: /jmrank [day/week/month] [页码]
        示例: /jmrank week 1
        """
        # 权限检查
        has_perm, error_msg = self._check_permission(event)
        if not has_perm:
            yield event.plain_result(error_msg)
            return

        # 验证排行榜类型
        ranking_type = str(ranking_type).lower().strip()
        if ranking_type not in ("day", "week", "month"):
            yield event.plain_result(
                "❌ 无效的排行榜类型\n用法: /jmrank [day/week/month] [页码]\n示例: /jmrank week 1"
            )
            return

        # 验证页码
        try:
            page = int(page)
            if page < 1:
                page = 1
        except (ValueError, TypeError):
            page = 1

        try:
            type_names = {"day": "日", "week": "周", "month": "月"}
            type_name = type_names.get(ranking_type, "周")
            yield event.plain_result(f"🏆 正在获取{type_name}排行榜第{page}页...")

            if ranking_type == "day":
                results = await self.browser.get_day_ranking(page)
            elif ranking_type == "week":
                results = await self.browser.get_week_ranking(page)
            else:
                results = await self.browser.get_month_ranking(page)

            # 限制结果数量
            page_size = self.config_manager.search_page_size
            results = results[:page_size]

            result_msg = MessageFormatter.format_ranking_results(
                results, ranking_type, page
            )
            yield event.plain_result(result_msg)

        except Exception as e:
            logger.error(f"获取排行榜失败: {e}")
            yield event.plain_result(MessageFormatter.format_error("network", str(e)))

    @filter.command("jmrec")
    async def recommend_command(
        self,
        event: AstrMessageEvent,
        arg1: str = None,
        arg2: str = None,
        arg3: str = None,
        arg4: str = None,
    ):
        """
        推荐浏览 - 按分类/排序/时间浏览漫画

        用法: /jmrec [分类] [排序] [时间] [页码]
        示例: /jmrec hanman hot week 1
        """
        # 权限检查
        has_perm, error_msg = self._check_permission(event)
        if not has_perm:
            yield event.plain_result(error_msg)
            return

        # 支持的参数值
        from .core.browser import JMBrowser

        categories = JMBrowser.get_category_list()
        orders = JMBrowser.get_order_list()
        times = JMBrowser.get_time_list()

        # 默认值
        category = "all"
        order_by = "hot"
        time_range = "week"
        page = 1

        # 追踪哪些参数类型已被显式设置
        category_set = False
        order_set = False
        time_set = False

        # 如果第一个参数是 help，显示帮助
        if arg1 and arg1.lower() == "help":
            yield event.plain_result(MessageFormatter.format_recommend_help())
            return

        # 智能解析参数（按顺序：分类 -> 排序 -> 时间 -> 页码）
        args = [arg1, arg2, arg3, arg4]
        for arg in args:
            if arg is None:
                continue

            arg_lower = str(arg).lower().strip()

            # 尝试解析为页码（纯数字）
            if arg_lower.isdigit():
                page = int(arg_lower)
                if page < 1:
                    page = 1
                continue

            # 尝试匹配分类
            if arg_lower in categories:
                if category_set:
                    yield event.plain_result(
                        f"❌ 检测到重复的分类参数: {arg}\n"
                        f"当前已设置分类为: {category}\n"
                        f"💡 每种类型只能指定一个参数"
                    )
                    return
                category = arg_lower
                category_set = True
                continue

            # 尝试匹配排序
            if arg_lower in orders:
                if order_set:
                    yield event.plain_result(
                        f"❌ 检测到重复的排序参数: {arg}\n"
                        f"当前已设置排序为: {order_by}\n"
                        f"💡 每种类型只能指定一个参数"
                    )
                    return
                order_by = arg_lower
                order_set = True
                continue

            # 尝试匹配时间
            if arg_lower in times:
                if time_set:
                    yield event.plain_result(
                        f"❌ 检测到重复的时间参数: {arg}\n"
                        f"当前已设置时间为: {time_range}\n"
                        f"💡 每种类型只能指定一个参数"
                    )
                    return
                time_range = arg_lower
                time_set = True
                continue

            # 未知参数，显示帮助提示
            yield event.plain_result(
                f"❌ 未知参数: {arg}\n💡 使用 /jmrec help 查看帮助"
            )
            return

        try:
            # 显示加载提示
            cat_name = MessageFormatter.CATEGORY_NAMES.get(category, category)
            order_name = MessageFormatter.ORDER_NAMES.get(order_by, order_by)
            time_name = MessageFormatter.TIME_NAMES.get(time_range, time_range)
            yield event.plain_result(
                f"🎯 正在获取 {cat_name} · {time_name}{order_name} 第{page}页..."
            )

            # 获取推荐内容
            results = await self.browser.get_category_albums(
                category=category,
                order_by=order_by,
                time_range=time_range,
                page=page,
            )

            # 限制结果数量
            page_size = self.config_manager.search_page_size
            results = results[:page_size]

            # 格式化并发送结果
            result_msg = MessageFormatter.format_recommend_results(
                results, category, order_by, time_range, page
            )
            yield event.plain_result(result_msg)

        except Exception as e:
            logger.error(f"获取推荐内容失败: {e}")
            if self.debug_mode:
                import traceback

                logger.error(traceback.format_exc())
            yield event.plain_result(MessageFormatter.format_error("network", str(e)))

    @filter.command("jmlogin")
    async def login_command(
        self, event: AstrMessageEvent, username: str = None, password: str = None
    ):
        """
        登录JM账号

        用法: /jmlogin <用户名> <密码>
        示例: /jmlogin myuser mypass
        """
        # 权限检查
        has_perm, error_msg = self._check_permission(event)
        if not has_perm:
            yield event.plain_result(error_msg)
            return

        # 参数检查
        if username is None or password is None:
            yield event.plain_result(
                "❌ 请提供用户名和密码\n用法: /jmlogin <用户名> <密码>\n示例: /jmlogin myuser mypass"
            )
            return

        try:
            yield event.plain_result("🔐 正在登录...")

            success, message = await self.auth_manager.login(username, password)

            if success:
                yield event.plain_result(f"✅ {message}")
            else:
                yield event.plain_result(f"❌ {message}")

        except Exception as e:
            logger.error(f"登录失败: {e}")
            yield event.plain_result(MessageFormatter.format_error("network", str(e)))

    @filter.command("jmlogout")
    async def logout_command(self, event: AstrMessageEvent):
        """
        登出JM账号

        用法: /jmlogout
        """
        # 权限检查
        has_perm, error_msg = self._check_permission(event)
        if not has_perm:
            yield event.plain_result(error_msg)
            return

        success, message = self.auth_manager.logout()

        if success:
            yield event.plain_result(f"✅ {message}")
        else:
            yield event.plain_result(f"❌ {message}")

    @filter.command("jmstatus")
    async def status_command(self, event: AstrMessageEvent):
        """
        查看登录状态

        用法: /jmstatus
        """
        # 权限检查
        has_perm, error_msg = self._check_permission(event)
        if not has_perm:
            yield event.plain_result(error_msg)
            return

        status = self.auth_manager.get_login_status()

        if status["logged_in"]:
            yield event.plain_result(f"✅ 已登录\n👤 用户名: {status['username']}")
        else:
            yield event.plain_result(
                "❌ 当前未登录\n💡 使用 /jmlogin <用户名> <密码> 登录"
            )

    @filter.command("jmfav")
    async def favorites_command(
        self, event: AstrMessageEvent, page: int = 1, folder_id: str = "0"
    ):
        """
        查看我的收藏

        用法: /jmfav [页码] [收藏夹ID]
        示例: /jmfav 1
        """
        # 权限检查
        has_perm, error_msg = self._check_permission(event)
        if not has_perm:
            yield event.plain_result(error_msg)
            return

        # 检查登录状态
        logged_in, login_msg = await self.auth_manager.ensure_logged_in()
        if not logged_in:
            yield event.plain_result(f"❌ {login_msg}\n💡 请先使用 /jmlogin 登录")
            return

        # 验证页码
        try:
            page = int(page)
            if page < 1:
                page = 1
        except (ValueError, TypeError):
            page = 1

        try:
            yield event.plain_result(f"⭐ 正在获取收藏夹第{page}页...")

            client = self.auth_manager.get_client()
            albums, folders = await self.browser.get_favorites(client, page, folder_id)

            result_msg = MessageFormatter.format_favorites(albums, folders, page)
            yield event.plain_result(result_msg)

        except Exception as e:
            logger.error(f"获取收藏夹失败: {e}")
            yield event.plain_result(MessageFormatter.format_error("network", str(e)))
