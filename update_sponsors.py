"""
爱发电赞助者数据同步脚本 - 简化版(仅显示头像+昵称)

从爱发电 API 获取赞助者和订单信息,生成 Markdown 格式的赞助者列表并更新到 README 文件中。
同时生成 JSON 格式的数据文件供其他应用使用。
"""
import hashlib
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import requests

# ==================== 配置常量 ====================
@dataclass(frozen=True)
class Config:
    """应用配置"""
    # API 配置
    ORDER_API: str = "https://afdian.com/api/open/query-order"
    SPONSOR_API: str = "https://afdian.com/api/open/query-sponsor"
    USER_ID: str = os.getenv("AFDIAN_USER_ID", "")
    TOKEN: str = os.getenv("AFDIAN_TOKEN", "")

    # 文件配置
    README_FILE: str = "README.md"
    JSON_FILE: str = "sponsor.json"
    MARKER_START: str = "<!-- AFDIAN_SPONSORS_START -->"
    MARKER_END: str = "<!-- AFDIAN_SPONSORS_END -->"

    # 显示配置
    AVATAR_SIZE: int = 50
    BEIJING_TZ: timezone = timezone(timedelta(hours=8))

    # 请求配置
    REQUEST_TIMEOUT: int = 15
    MAX_PAGES: int = 2000
    MAX_RETRIES: int = 3
    BACKOFF_BASE: float = 0.8
    PAGE_SIZE: int = 50


config = Config()

# ==================== 日志配置 ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)


# ==================== API 客户端 ====================
class AfdianAPIClient:
    """爱发电 API 客户端"""

    def __init__(self, user_id: str, token: str):
        self.user_id = user_id
        self.token = token
        self.session = requests.Session()

    def _make_sign(self, params_str: str, ts: int) -> str:
        """生成 API 签名"""
        raw = f"{self.token}params{params_str}ts{ts}user_id{self.user_id}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def _post_with_retry(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """带重试机制的 POST 请求"""
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                response = self.session.post(
                    url,
                    json=payload,
                    timeout=config.REQUEST_TIMEOUT
                )
                response.raise_for_status()
                return response.json()
            except Exception as e:
                if attempt == config.MAX_RETRIES:
                    raise
                wait = (config.BACKOFF_BASE ** attempt) + random.random() * 0.5
                logger.warning(
                    f"请求失败(尝试 {attempt}/{config.MAX_RETRIES}): {e}, "
                    f"等待 {wait:.2f}s 后重试"
                )
                time.sleep(wait)

        raise RuntimeError("Unexpected control flow")

    def _fetch_page(self, api_url: str, page: int, per_page: int) -> Optional[dict[str, Any]]:
        """获取单页数据"""
        params = {"page": page, "per_page": per_page}
        params_str = json.dumps(params, separators=(",", ":"), ensure_ascii=False)
        ts = int(time.time())
        sign = self._make_sign(params_str, ts)

        payload = {
            "user_id": self.user_id,
            "params": params_str,
            "ts": ts,
            "sign": sign
        }

        try:
            data = self._post_with_retry(api_url, payload)

            if not isinstance(data, dict):
                logger.error(f"第 {page} 页返回非 JSON 结构")
                return None

            if data.get("ec") != 200:
                logger.error(
                    f"第 {page} 页接口返回错误: ec={data.get('ec')}, "
                    f"em={data.get('em')}"
                )
                return None

            return data
        except Exception as e:
            logger.error(f"第 {page} 页请求失败: {e}")
            return None

    def fetch_all_pages(self, api_url: str) -> list[dict[str, Any]]:
        """获取所有分页数据"""
        all_items = []
        page = 1

        while page <= config.MAX_PAGES:
            data = self._fetch_page(api_url, page, config.PAGE_SIZE)

            if not data:
                break

            items = data.get("data", {}).get("list", [])
            if not isinstance(items, list):
                logger.warning(f"第 {page} 页 list 不是数组")
                break

            logger.info(f"第 {page} 页获取 {len(items)} 条记录")
            all_items.extend(items)

            total_page = data.get("data", {}).get("total_page")
            if total_page and page >= total_page:
                break

            if not items:
                break

            page += 1

        return all_items

    def fetch_sponsors(self) -> list[dict[str, Any]]:
        """获取所有赞助者"""
        logger.info("开始抓取赞助者列表")
        return self.fetch_all_pages(config.SPONSOR_API)

    def fetch_orders(self) -> list[dict[str, Any]]:
        """获取所有订单"""
        logger.info("开始抓取订单列表")
        return self.fetch_all_pages(config.ORDER_API)


# ==================== 数据处理 ====================
class SponsorDataProcessor:
    """赞助者数据处理器"""

    def __init__(self, sponsors: list[dict[str, Any]], orders: list[dict[str, Any]]):
        self.sponsors = sponsors
        self.orders = orders
        self.user_map = self._build_user_map()

    def _build_user_map(self) -> dict[str, dict[str, str]]:
        """构建用户 ID 到用户信息的映射"""
        user_map = {}
        for sponsor in self.sponsors:
            try:
                user = sponsor.get("user", {})
                user_id = user.get("user_id")
                if user_id:
                    user_map[user_id] = {
                        "name": user.get("name") or "-",
                        "avatar": user.get("avatar") or "",
                    }
            except Exception as e:
                logger.warning(f"处理赞助者项异常: {e}")
        return user_map

    @staticmethod
    def _safe_text(value: Any) -> str:
        """清理文本,避免破坏 Markdown 表格"""
        if value is None:
            return "-"
        text = str(value).replace("\n", " ").strip()
        text = text.replace("|", "&#124;")
        return text if text else "-"

    @staticmethod
    def _get_order_timestamp(order: dict[str, Any]) -> int:
        """获取订单时间戳"""
        try:
            return int(order.get("last_pay_time") or order.get("create_time") or 0)
        except Exception:
            return 0

    @staticmethod
    def _format_timestamp(timestamp: int) -> str:
        """格式化时间戳为北京时间"""
        if not timestamp:
            return "-"
        dt = datetime.fromtimestamp(timestamp, config.BEIJING_TZ)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def _generate_table_row(self, order: dict[str, Any]) -> Optional[str]:
        """生成表格行(仅包含头像和昵称)"""
        try:
            user_id = order.get("user_id")
            user_info = self.user_map.get(user_id, {})

            name = self._safe_text(
                user_info.get("name") or order.get("user_name") or "-"
            )
            avatar = user_info.get("avatar") or order.get("avatar") or ""

            avatar_cell = (
                f'<img src="{avatar}" width="{config.AVATAR_SIZE}">'
                if avatar else "-"
            )

            return f"| {avatar_cell} | {name} |"
        except Exception as e:
            logger.warning(f"处理订单行时异常: {e}")
            return None

    def _generate_sponsor_item(self, order: dict[str, Any]) -> Optional[dict[str, Any]]:
        """生成 JSON 格式的赞助者条目(仅包含头像和昵称)"""
        try:
            user_id = order.get("user_id")
            user_info = self.user_map.get(user_id, {})

            name = user_info.get("name") or order.get("user_name") or "匿名"
            avatar = user_info.get("avatar") or order.get("avatar") or ""
            timestamp = self._get_order_timestamp(order)

            return {
                "user_id": user_id,
                "name": name,
                "avatar": avatar,
                "timestamp": timestamp,
                "time": self._format_timestamp(timestamp)
            }
        except Exception as e:
            logger.warning(f"生成 JSON 条目时异常: {e}")
            return None

    def generate_markdown(self) -> str:
        """生成 Markdown 格式的赞助者列表(仅包含头像和昵称)"""
        sorted_orders = sorted(
            self.orders,
            key=self._get_order_timestamp,
            reverse=True
        )

        now = datetime.now(config.BEIJING_TZ)
        update_time = now.strftime("%Y-%m-%d %H:%M:%S")

        lines = [
            "## ❤️ 赞助者列表",
            "",
            f"> 更新时间: {update_time} (UTC+8) 每4小时更新一次",
            "",
            "| 头像 | 昵称 |",
            "|------|------|",
        ]

        skipped = 0
        for order in sorted_orders:
            row = self._generate_table_row(order)
            if row:
                lines.append(row)
            else:
                skipped += 1

        if skipped:
            logger.info(f"共跳过 {skipped} 条无法处理的订单")

        return "\n".join(lines)

    def generate_json_data(self) -> dict[str, Any]:
        """生成 JSON 格式的赞助者数据(仅包含头像和昵称)"""
        sorted_orders = sorted(
            self.orders,
            key=self._get_order_timestamp,
            reverse=True
        )

        now = datetime.now(config.BEIJING_TZ)
        update_time = now.strftime("%Y-%m-%d %H:%M:%S")

        sponsors_list = []
        for order in sorted_orders:
            item = self._generate_sponsor_item(order)
            if item:
                sponsors_list.append(item)

        return {
            "update_time": update_time,
            "update_timestamp": int(now.timestamp()),
            "total_count": len(sponsors_list),
            "sponsors": sponsors_list
        }


# ==================== README 更新 ====================
class ReadmeUpdater:
    """README 文件更新器"""

    def __init__(self, filepath: str):
        self.filepath = Path(filepath)

    def update(self, content: str) -> None:
        """更新 README 中的标记区块"""
        if not self.filepath.exists():
            self._create_new_file(content)
            return

        text = self.filepath.read_text(encoding="utf-8")

        if config.MARKER_START not in text or config.MARKER_END not in text:
            self._append_markers(text, content)
            return

        self._replace_content(text, content)

    def _create_new_file(self, content: str) -> None:
        """创建新的 README 文件"""
        logger.warning(f"{self.filepath} 不存在,创建新文件")
        new_content = f"{config.MARKER_START}\n{content}\n{config.MARKER_END}\n"
        self.filepath.write_text(new_content, encoding="utf-8")

    def _append_markers(self, text: str, content: str) -> None:
        """在文件末尾追加标记区块"""
        logger.warning(f"{self.filepath} 中未找到占位标记,自动追加")
        new_text = (
                text.rstrip() +
                f"\n\n{config.MARKER_START}\n{content}\n{config.MARKER_END}\n"
        )
        self.filepath.write_text(new_text, encoding="utf-8")

    def _replace_content(self, text: str, content: str) -> None:
        """替换标记区块中的内容"""
        before = text.split(config.MARKER_START)[0]
        after = text.split(config.MARKER_END)[1]
        new_block = f"{config.MARKER_START}\n{content}\n{config.MARKER_END}"
        self.filepath.write_text(before + new_block + after, encoding="utf-8")
        logger.info(f"已更新 {self.filepath} 中的标记区块")


# ==================== JSON 文件生成 ====================
class JsonExporter:
    """JSON 数据导出器"""

    def __init__(self, filepath: str):
        self.filepath = Path(filepath)

    def export(self, data: dict[str, Any]) -> None:
        """导出 JSON 数据到文件"""
        try:
            self.filepath.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            logger.info(f"已生成 JSON 文件: {self.filepath}")
        except Exception as e:
            logger.error(f"生成 JSON 文件失败: {e}")
            raise


# ==================== 主函数 ====================
def main() -> None:
    """主函数"""
    # 验证配置
    if not config.USER_ID or not config.TOKEN:
        logger.error("请先设置环境变量 AFDIAN_USER_ID 和 AFDIAN_TOKEN")
        return

    # 初始化 API 客户端
    client = AfdianAPIClient(config.USER_ID, config.TOKEN)

    # 获取数据
    try:
        sponsors = client.fetch_sponsors()
        logger.info(f"抓取到 {len(sponsors)} 个赞助者记录")
    except Exception as e:
        logger.error(f"抓取赞助者时发生错误: {e}")
        sponsors = []

    try:
        orders = client.fetch_orders()
        logger.info(f"抓取到 {len(orders)} 条订单记录")
    except Exception as e:
        logger.error(f"抓取订单时发生错误: {e}")
        return

    if not orders:
        logger.error("没有订单数据,无法生成列表")
        return

    # 处理数据并生成 Markdown 和 JSON
    processor = SponsorDataProcessor(sponsors, orders)

    # 生成 Markdown
    markdown = processor.generate_markdown()

    # 更新 README
    try:
        updater = ReadmeUpdater(config.README_FILE)
        updater.update(markdown)
        logger.info(f"已更新 README 文件")
    except Exception as e:
        logger.error(f"更新 README 失败: {e}")

    # 生成 JSON 文件
    try:
        json_data = processor.generate_json_data()
        exporter = JsonExporter(config.JSON_FILE)
        exporter.export(json_data)
        logger.info(f"脚本完成,共处理 {len(orders)} 条订单")
    except Exception as e:
        logger.error(f"生成 JSON 文件失败: {e}")


if __name__ == "__main__":
    main()