"""群聊图片识别：收到运动记录截图后 OCR 提取数据并回显。

未绑定平台的成员：识别结果会记入当日明细（供排行）+ 累计里程。
"""

import asyncio
import base64
import hashlib
import time
from io import BytesIO
from pathlib import Path

import httpx
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, MessageEvent
from nonebot.log import logger
from nonebot.rule import Rule
from PIL import Image, ImageOps

from ..db import get_session
from ..models.member import Member
from ..services import checkin, cheers, llm, net, parsers, ranking, sync, timeutil
from ..services.member import get_or_create_member
from ..services.ocr import recognize_boxes
from ..services.providers.base import DailyStats
from .admin import notify_gift_claim


def _has_image(event) -> bool:
    """仅当消息含图片段时才触发，避免纯文本消息也空转一次 handler。"""
    return any(s.type == "image" for s in event.get_message())


image_matcher = on_message(priority=10, block=False, rule=Rule(_has_image))

# 保存收到的图片，便于排查 OCR 识别误差
IMG_DIR = Path("data/images")
# 只保留最近 N 张原始截图，避免排查用图片长期累积占满磁盘
_IMG_KEEP = 500


def _cleanup_images(keep: int = _IMG_KEEP) -> None:
    """只保留最近 keep 张原始截图，删除更早的（按 mtime）。"""
    try:
        files = sorted(
            IMG_DIR.glob("*.jpg"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        return
    for f in files[keep:]:
        try:
            f.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"清理旧截图失败 {f}: {e}")


async def _download(bot: Bot, seg) -> bytes:
    """下载图片字节。优先 get_image API，其次直接下载 url。

    TLS 校验按地址区分：内网/本机地址（NapCat 自签/HTTP）降级不校验，
    公网地址保持校验，避免中间人替换图片内容。
    """
    file = seg.data.get("file", "")
    url = seg.data.get("url", "")
    try:
        res = await bot.get_image(file=file)
        if isinstance(res, dict):
            f = res.get("file", "")
            if isinstance(f, str) and f.startswith("base64://"):
                return base64.b64decode(f[len("base64://") :])
            if isinstance(f, str) and f and Path(f).exists():
                return Path(f).read_bytes()
            u = res.get("url")
            if u:
                async with httpx.AsyncClient(timeout=30, verify=net.verify_tls(u)) as c:
                    r = await c.get(u)
                    r.raise_for_status()
                    return r.content
    except Exception as e:
        logger.warning(f"get_image 失败，尝试直接下载 url: {e}")
    if url:
        # 兜底分支同样需要包异常：公网图床/证书过期或自签会抛 SSLError（TLS 校验已按地址
        # 开启），不包会让异常冒泡到 handle_image，用户收不到任何回复。失败按既有语义返回空。
        try:
            async with httpx.AsyncClient(timeout=30, verify=net.verify_tls(url)) as c:
                r = await c.get(url)
                r.raise_for_status()
                return r.content
        except Exception as e:
            logger.warning(f"直接下载图片 url 失败: {e}")
    return b""


# 表情包/缩略图的最长边阈值：QQ 表情包通常 ≤ 400px，手机运动截图 ≥ 1280px
_MIN_IMAGE_EDGE = 500
# 图片大小上限（20MB）：超大文件直接跳过，防 OCR/视觉模型吃内存
_MAX_IMAGE_BYTES = 20 * 1024 * 1024


def _is_too_small(img_bytes: bytes) -> bool:
    """读图片尺寸（Pillow 惰性读 header，不加载像素），判断是否表情包/缩略图。

    读不出尺寸时返回 False（不拦截），交给后续 OCR 结果判据兜底。
    """
    try:
        with Image.open(BytesIO(img_bytes)) as im:
            # 应用 EXIF 转置：部分相机/机型会横拍，不转置会误判长短边
            w, h = ImageOps.exif_transpose(im).size
        return max(w, h) < _MIN_IMAGE_EDGE
    except Exception as e:
        logger.warning(f"读取图片尺寸失败: {e}")
        return False


def _format_data(data: dict) -> list:
    # 值 >0 才显示（统一在 cheers.format_stat_lines 内处理），避免把未识别的 0 值刷屏。
    return cheers.format_stat_lines(data)


def _format_cheer(data: dict) -> str:
    """把识别结果渲染成鼓励语气回复：开场/结尾按数据上下文选取（集中在 cheers 库）。"""
    lines = _format_data(data)
    return cheers.opener(data) + "\n" + "\n".join(lines) + "\n\n" + cheers.closer(data)


# ---------------------------------------------------------------------------
# 重复打卡去重（内存态，24 小时窗口，全局去重）
# 同一张图（MD5 相同）无论本人重发还是群里他人转发，只累计一次，
# 距离/爬升/时长/消耗等全部字段都不重复计入。
# ---------------------------------------------------------------------------

_RECENT_IMAGES: dict[str, float] = {}  # md5 -> 记录时间戳
_RECENT_DHASH: dict[str, float] = {}  # 感知哈希 -> 记录时间戳（转发二次压缩仍能判重）
_DUP_WINDOW_SEC = 86400  # 24 小时


def _purge(table: dict[str, float]) -> None:
    now = time.time()
    for k in [k for k, ts in table.items() if now - ts > _DUP_WINDOW_SEC]:
        table.pop(k, None)


def _dhash(img_bytes: bytes, size: int = 9) -> str:
    """感知哈希（dHash）：灰度缩放到 (size+1)×size，逐行比较相邻像素得 64 位指纹。

    与 MD5（字节级）互补：转发/重传造成的二次压缩会改字节但视觉不变，dHash 仍能判重。
    计算失败返回空串（退化到只按 MD5 判重）。
    """
    try:
        with Image.open(BytesIO(img_bytes)) as im:
            im = (
                ImageOps.exif_transpose(im)
                .convert("L")
                .resize((size + 1, size), Image.Resampling.LANCZOS)
            )
            px = list(im.getdata())
    except Exception as e:
        logger.warning(f"dHash 计算失败: {e}")
        return ""
    bits = 0
    width = size + 1
    for y in range(size):
        row = y * width
        for x in range(size):
            bits = (bits << 1) | (1 if px[row + x + 1] > px[row + x] else 0)
    return format(bits, "x")


def _is_duplicate(md5: str, dhash: str = "") -> bool:
    """同一张图在窗口期内重发（含群内转发）视为重复；顺带惰性清理过期项。"""
    _purge(_RECENT_IMAGES)
    _purge(_RECENT_DHASH)
    if md5 in _RECENT_IMAGES:
        return True
    return bool(dhash) and dhash in _RECENT_DHASH


def _mark_seen(md5: str, dhash: str = "") -> None:
    now = time.time()
    _RECENT_IMAGES[md5] = now
    if dhash:
        _RECENT_DHASH[dhash] = now


@image_matcher.handle()
async def handle_image(bot: Bot, event: MessageEvent):
    segs = [s for s in event.get_message() if s.type == "image"]
    if not segs:
        return

    qq = event.get_user_id()
    name = getattr(getattr(event, "sender", None), "nickname", None) or qq
    img_bytes = await _download(bot, segs[0])
    if not img_bytes:
        logger.warning(f"[图片识别] qq={qq} 图片下载失败，静默跳过")
        return

    # 大小上限：超大文件直接跳过，防 OCR/视觉模型吃内存
    if len(img_bytes) > _MAX_IMAGE_BYTES:
        logger.warning(f"[图片识别] qq={qq} 图片过大（{len(img_bytes)} bytes），跳过")
        return

    # 表情包/缩略图：尺寸过小，直接跳过（不 OCR、不落盘、不响应）
    if _is_too_small(img_bytes):
        logger.info(f"[图片识别] qq={qq} 疑似表情包/小图，跳过")
        return

    # 图片指纹：用于全局去重（见 _is_duplicate）；md5 字节级 + dHash 感知级
    img_md5 = hashlib.md5(img_bytes).hexdigest()
    img_dhash = _dhash(img_bytes)

    # 先读绑定状态：已绑定成员的截图只回显、不写库，既没有「重复累计」风险，
    # 「刚才记过啦」的提示对他们也不成立；因此去重只对未绑定成员执行。
    # 这里提前查一次，供「去重范围」「跳过 vision 上传」「已绑定只回显不写库」三处共用，
    # 避免重复开 session（也修掉了原先「去重在前、读绑定在后」的语义错位）。
    session = get_session()
    try:
        member = session.get(Member, qq)
        bound = member is not None and bool(member.platform)
        if member is not None:
            name = member.display_name
    finally:
        session.close()

    # 重复打卡去重（仅未绑定成员）：同一张图（含群内转发）已成功记过，直接跳过。
    # 前置到 OCR/视觉之前，既防重复累计，也避免重复截图反复消耗本地 OCR 与视觉调用。
    if not bound and _is_duplicate(img_md5, img_dhash):
        logger.info(f"[图片识别] qq={qq} 检测到重复截图，跳过识别")
        await image_matcher.finish("⏳ 这张截图刚才已经记过啦，本次不重复累计")

    # 保存原始图片，便于排查 OCR 识别误差
    try:
        IMG_DIR.mkdir(parents=True, exist_ok=True)
        (IMG_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}_{qq}.jpg").write_bytes(img_bytes)
        _cleanup_images()
    except Exception as e:
        logger.warning(f"保存图片失败: {e}")

    # 本地 OCR 识别（主路径，数据不出本机）。
    try:
        result = await asyncio.to_thread(recognize_boxes, img_bytes)
    except Exception as e:
        logger.warning(f"[图片识别] qq={qq} OCR 识别失败: {e}")
        result = []

    async def _vision_fallback() -> dict:
        """视觉模型兜底识别；未配置 / 关闭 / 失败一律返回空 dict（截图不出本机）。

        受 settings.llm_vision_enabled 与 LLM_API_KEY 双重闸门控制（见 llm.vision_extract），
        仅在「OCR 读不出距离」或「识别结果可疑」时调用，绝不无条件上传每张截图。
        """
        try:
            return await asyncio.to_thread(llm.vision_extract, img_bytes) or {}
        except Exception as e:
            logger.warning(f"[图片识别] qq={qq} 视觉识别失败: {e}")
            return {}

    # 解析兜底：解析器异常不应拖垮整条 handler，静默跳过即可
    try:
        text = "\n".join(str(item[1]) for item in result)
        # 主路径：按框坐标做「标签—数值」空间关联；无结果时退回纯文本解析
        ocr_data = parsers.parse_activity_from_boxes(result)
        if not ocr_data:
            ocr_data = parsers.parse_activity(text)

        data = dict(ocr_data)

        # 视觉模型仅在本地 OCR 读不出核心字段（距离）时才兜底，绝不无条件上传每张截图：
        # 落实「截图不出本机」的对外承诺，也避免无效/重复截图反复消耗视觉调用。
        # 已绑定成员（数据以 App 为准）连兜底也不触发——为一句回显上传截图不值得。
        # 用 get() 而非 in：距离「存在但为 0」同样视为没读出，否则不会触发兜底。
        vision_data: dict = {}
        vision_used = False
        if not ocr_data.get("distance_km") and not bound:
            vision_data = await _vision_fallback()
            vision_used = True  # 已就该图咨询过 vision，可疑复核不重复调用

        # 合并：以本地 OCR 为准，缺失/0 的字段用视觉结果补齐
        for k, v in vision_data.items():
            if k not in data or not data.get(k):
                data[k] = v

        # 比对：两侧都有值且相对误差超阈值 → 视觉模型结合 OCR 二次看图给确定值
        conflicts = parsers.find_field_conflicts(ocr_data, vision_data)
        if conflicts:
            recon = (
                await asyncio.to_thread(
                    llm.vision_reconcile, img_bytes, ocr_data, vision_data, conflicts
                )
                or {}
            )
            for k in conflicts:
                if k in recon:
                    data[k] = recon[k]
            logger.info(
                f"[图片识别] qq={qq} 识别比对分歧 {conflicts}："
                f"OCR={ {k: ocr_data.get(k) for k in conflicts} }，"
                f"视觉={ {k: vision_data.get(k) for k in conflicts} }，复核={recon}"
            )
    except Exception as e:
        logger.warning(f"[图片识别] qq={qq} 解析失败: {e}")
        return
    logger.info(f"[图片识别] qq={qq}\nOCR 原文:\n{text}\n解析结果: {data}")

    # 页面类型分析：统计/列表页（月汇总、活动列表等）展示的是多条运动的聚合，
    # 不是单次运动详情，不应作为一次运动回显/落库。先分析再确认，避免误读。
    page_kind = parsers.detect_page_kind(text)
    if page_kind == "summary":
        logger.info(f"[图片识别] qq={qq} 识别为统计/列表页，拒绝记录")
        await image_matcher.finish(
            "🔍 我识别出这是一张「统计/列表」页（多条运动的汇总），不是单次运动详情，所以先不记录。\n"
            "· 若要记录某一次运动：点开那条记录，发**单次运动详情页**截图（含这一次的距离/时长/配速）；\n"
            "· 若要查自己的周/月汇总：直接发「周数据」「月数据」，我会按平台接口自动统计。"
        )

    # 可疑图补一次视觉兜底：OCR 读出了距离但数值可疑（如 5.20 km 被读成 52.0 km →
    # 距离/时长推出的速度明显超限；或每公里消耗离谱），说明识别质量存疑。此时若本次
    # 还没咨询过 vision、且非绑定成员，只对这一张可疑图补一次视觉 + 分歧复核：
    # 只有可疑图才多一次调用（成本可控），换准确性受益（避免错值直接污染排行与累计里程）。
    # 必须在 sanitize_activity 之前判定——sanitize 会把超限的时长/卡路里丢掉，
    # 届时判定所需的信息已丢失。
    signals = parsers.suspicious_signals(data)
    if signals and not vision_used and not bound:
        logger.info(f"[图片识别] qq={qq} 识别结果可疑（{'；'.join(signals)}），补一次视觉兜底")
        fallback = await _vision_fallback()
        if fallback:
            for k, v in fallback.items():
                if k not in data or not data.get(k):
                    data[k] = v
            conflicts = parsers.find_field_conflicts(data, fallback)
            if conflicts:
                recon = (
                    await asyncio.to_thread(
                        llm.vision_reconcile, img_bytes, data, fallback, conflicts
                    )
                    or {}
                )
                for k in conflicts:
                    if k in recon:
                        data[k] = recon[k]
                logger.info(
                    f"[图片识别] qq={qq} 可疑复核分歧 {conflicts}："
                    f"合并前={ {k: data.get(k) for k in conflicts} }，复核={recon}"
                )

    # 合理性校验：OCR 偶发把步数 / 卡路里 / 统计页总量误识别成距离，落库前先清洗+拦截，
    # 超范围的夸张跑量直接拒绝，避免污染排行与累计里程（这是 detect_page_kind 之外的最后防线）。
    data, reject_reason = parsers.sanitize_activity(data)
    if reject_reason:
        logger.info(f"[图片识别] qq={qq} 数据不合理，拒绝记录: {reject_reason}")
        await image_matcher.finish(
            f"⚠️ {reject_reason}，本次未记录。\n"
            "若是单次运动详情，请重发一张更清晰的截图；若是多天汇总，直接发「周数据 / 月数据」查看。"
        )

    if not any(
        k in data
        for k in (
            "distance_km",
            "steps",
            "calories",
            "active_minutes",
            "sleep_hours",
            "ascent_meters",
            "avg_pace_sec_per_km",
            "avg_hr",
        )
    ):
        # 读不出运动字段时的交互分级：
        #   - 已绑定成员：截图本就不记录（数据以 App 为准），静默跳过；
        #   - OCR 零文字且视觉也无果（纯照片/风景大图）：静默，避免对群内大图产生噪音；
        #   - 其余（OCR 有文字但读不出运动字段）：回复引导，不再让用户「发了图没反应」。
        if bound or (not result and not vision_data):
            logger.info(f"[图片识别] qq={qq} 未识别出运动数据，静默跳过")
            return
        logger.info(f"[图片识别] qq={qq} 未识别出运动数据，回复引导")
        await image_matcher.finish(
            "🔍 没认出这张图里的运动数据…\n"
            "· 记录单次运动：请发**单次运动详情页**截图（含这一次的距离/时长/配速）；\n"
            "· 查汇总数据：直接发「周数据」「月数据」，我会按平台接口自动统计。"
        )

    # 已绑定平台 → 只展示不写库（数据已由平台接口自动同步）；
    # 未绑定平台 → 累加累计里程 + 写当日明细（供排行，含爬升/配速等）。
    # bound / name 已在 OCR 之前读取（见上），此处直接复用。
    if bound:
        # 已绑定平台：截图仅回显不写库（数据已由平台接口自动同步），并明确告知
        # 「以 App 为准」，避免用户误以为截图又被记了一次、结果「今日」却没变化。
        await image_matcher.finish(
            _format_cheer(data)
            + "\n\n📌 你已绑定平台，本次截图未计入数据，今日与排行均以 App 同步为准"
        )
    if "distance_km" not in data:
        await image_matcher.finish(_format_cheer(data))

    # 「今日群内第 N 名」需全群当天数据齐全：先同步已绑定成员的今日数据（带节流，
    # 10 分钟内复用上次结果），让截图成员的名次与绑定成员同口径——否则会漏掉今天
    # 还没查询过的绑定成员，出现「截图与 App 各自算、名次对不上」的问题。
    try:
        await asyncio.to_thread(sync.sync_today_all_throttled)
    except Exception as e:
        logger.warning(f"[图片识别] qq={qq} 同步全员今日数据失败（今日名次可能不全）: {e}")

    # 写库：SQLite 写入极快，直接在事件循环内同步执行，避免把同一个 session 传进
    # asyncio.to_thread（不同线程共享 Session 违反 SQLAlchemy 线程安全约定）。
    milestone_hits: list[int] = []
    fest = None
    gift_rank: int | None = None
    lottery_result: tuple[int, list[str]] | None = None
    streak = 0
    today_rank: int | None = None
    session = get_session()
    try:
        member = get_or_create_member(session, qq, name)
        total = sync.add_manual_distance(member, data["distance_km"], session)
        # 截图记录也进当日明细，让未绑定成员出现在每日排行里。
        # parsers 返回 dict，统一转成 DailyStats（过滤非模型字段，防御未来新增键）。
        today_d = timeutil.today()
        stats = DailyStats(
            date=today_d,
            **{k: v for k, v in data.items() if k in DailyStats.model_fields},
        )
        sync.record_manual_activity(member, today_d, stats, session)
        sync.log_checkin(
            qq,
            today_d,
            data["distance_km"],
            session,
            ascent_meters=data.get("ascent_meters", 0.0),
            calories=data.get("calories", 0),
            active_minutes=data.get("active_minutes", 0),
        )

        # 打卡彩蛋：里程碑 / 礼物 / 节日+特殊距离 / 群抽奖（幂等，状态表保证只触发一次）
        checkin_total = checkin.total_days(qq, session)
        milestone_hits = checkin.crossed_milestones(qq, checkin_total, session)
        gift_rank = checkin.auto_gift(qq, checkin_total, session)
        fest = checkin.check_festival(qq, today_d, data.get("distance_km", 0.0), session)
        lt = checkin.check_lottery(session)
        if lt is not None:
            winners = checkin.draw_lottery(session)
            winner_names = []
            for w in winners:
                m = session.get(Member, w)
                winner_names.append(m.display_name if m else w)
            lottery_result = (lt, winner_names)
        session.commit()
        # 连续打卡 + 今日名次：落库后同 session 读，保证含本次刚写入的记录
        streak = checkin.current_streak(qq, session)
        today_rank = ranking.today_distance_rank(qq, session)
        # 全部落库成功后才标记去重：commit 失败不会漏标，重发仍能补记
        _mark_seen(img_md5, img_dhash)
    except Exception as e:
        logger.exception(f"[图片识别] qq={qq} 记录失败: {e}")
        await image_matcher.finish("记录失败，请稍后重试，或发「帮助」看我能做什么")
    finally:
        session.close()

    reply = (
        _format_cheer(data)
        + f"\n\n✅ 已记入今日数据：本次 +{data['distance_km']} km，累计 {total} km，发「今日」即可查看"
    )
    if streak:
        note = cheers.streak_note(streak)
        line = f"🔥 连续打卡 {streak} 天"
        if note:
            line += f"（{note}）"
        reply += f"\n{line}"
    if today_rank is not None:
        reply += f"\n🏅 今日群内第 {today_rank} 名"
    # 里程碑彩蛋：AI 一句祝贺，失败降级模板
    for m in milestone_hits:
        cheer = await asyncio.to_thread(llm.milestone_cheer, name, m)
        reply += f"\n\n🎉 {cheer or f'达成第 {m} 次打卡里程碑，坚持就是胜利！'}"
    # 第 GIFT_DAYS 天礼物：自动领取（先到先得），并通知团长
    if gift_rank is not None:
        reply += (
            f"\n\n🎁 恭喜 {name}！你是第 {gift_rank} 位达成第 {checkin.GIFT_DAYS} 天打卡的跑友，"
            "自动领取「小红书惊喜小礼物」，已通知团长安排发货～"
        )
        await notify_gift_claim(bot, qq, name, gift_rank, getattr(event, "group_id", None))
    # 节日+特殊距离彩蛋
    if fest is not None:
        km = data["distance_km"]
        fallback = f"{fest['name']}快乐！{km} km 跑得漂亮，继续加油～"
        cheer = await asyncio.to_thread(llm.festival_cheer, name, fest["name"], km)
        reply += f"\n\n🎊 {cheer or fallback}"
    # 大模型补一句鼓励：失败返回 None，自动降级为纯数据回显，不影响主链路
    cheer = await asyncio.to_thread(llm.checkin_cheer, name, data)
    if cheer:
        reply += f"\n\n💪 {cheer}"

    # 群抽奖开奖：公告到当前群（状态表保证只触发一次）
    if lottery_result is not None:
        lt, winners = lottery_result
        announce = (
            f"🎉 全群累计打卡突破 {lt} 天！群抽奖开奖：{'、'.join(winners)} 获得{checkin.LOTTERY_PRIZE}～"
            f"恭喜这位跑友！"
        )
        gid = getattr(event, "group_id", None)
        if gid is not None:
            try:
                await bot.send_group_msg(group_id=gid, message=announce)
            except Exception as e:
                logger.warning(f"群抽奖公告发送失败: {e}")

    await image_matcher.finish(reply)
