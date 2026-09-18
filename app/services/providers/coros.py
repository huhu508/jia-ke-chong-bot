import base64
import hashlib
import json
import re
import secrets
import time
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .base import DailyStats, SportProvider


class CorosProvider(SportProvider):
    """COROS 高驰（官方 MCP + OAuth 2.0 授权码 + PKCE）。

    通过 COROS 官方 MCP 的 ``queryDailyHealthData`` 工具读取步数 / 卡路里 /
    睡眠等全天指标。用户在自己的浏览器里完成授权（OAuth），机器人不接触账号
    密码、不顶掉手机 App 登录态。

    token 按 QQ 持久化到 ``data/accounts/coros_<qq>.json``，过期用 refresh_token 自动刷新。
    """

    name = "coros"

    ISSUERS = {
        "cn": "https://mcpcn.coros.com",
        "us": "https://mcp.coros.com",
    }
    REDIRECT_URI = "http://127.0.0.1:43123/callback"
    SCOPE = "openid offline_access mcp.tools"
    CLIENT_NAME = "jia-ke-chong-bot"
    _UA = {"User-Agent": "jia-ke-chong-bot/0.1.0"}

    _DIR = Path("data/accounts")

    def __init__(self, region: str = "cn"):
        self._region = region
        self._issuer = self.ISSUERS.get(region, self.ISSUERS["cn"])
        self._mcp_url = f"{self._issuer}/mcp"

    @property
    def configured(self) -> bool:
        # OAuth 无需预先配置账号密码，始终可发起授权
        return True

    def _token_path(self, qq: str) -> Path:
        return self._DIR / f"coros_{qq}.json"

    def _pending_path(self, qq: str) -> Path:
        return self._DIR / f"coros_pending_{qq}.json"

    # ------------------------------------------------------------------
    # 授权流程
    # ------------------------------------------------------------------

    def start_auth(self, extra: Optional[dict] = None) -> str:
        """发起 OAuth 授权，返回用户需在浏览器打开的登录链接（约 5 分钟有效）。"""
        client_id = self._register_client()
        verifier = self._pkce_verifier()
        challenge = self._pkce_challenge(verifier)
        state = secrets.token_urlsafe(24)

        authorize_url = f"{self._issuer}/oauth2/authorize?" + urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": self.REDIRECT_URI,
                "scope": self.SCOPE,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": self._mcp_url,
                "state": state,
            }
        )

        cli = httpx.post(
            f"{self._issuer}/api/v1/cli/login-sessions",
            json={"clientId": client_id},
            headers={**self._UA, "Content-Type": "application/json"},
            timeout=30,
        ).json()

        qq = (extra or {}).get("qq", "unknown")
        pending = {
            "client_id": client_id,
            "verifier": verifier,
            "state": state,
            "authorize_url": authorize_url,
            "session_id": cli["sessionId"],
            "poll_token": cli["pollToken"],
            "login_url": cli["loginUrl"],
            "poll_interval": int(cli.get("intervalSeconds", 3) or 3),
            "extra": extra or {},
        }
        self._save_json(self._pending_path(qq), pending)
        return cli["loginUrl"]

    def finish_auth(self, qq: str, timeout: float = 15.0) -> dict:
        """轮询授权结果，成功则换取并持久化到该 QQ 的 token 文件。

        返回 ``{"authorized": True}``（已授权）或 ``{"authorized": False}``（尚未授权）。
        """
        pending = self._load_json(self._pending_path(qq))
        if not pending:
            raise RuntimeError("没有待确认的授权，请先发送「绑定 coros」")

        deadline = time.monotonic() + timeout
        login_ticket = None
        authorized = False
        while time.monotonic() < deadline:
            resp = httpx.post(
                f"{self._issuer}/api/v1/cli/login-sessions/{pending['session_id']}/claim",
                headers={**self._UA, "X-Poll-Token": pending["poll_token"]},
                timeout=30,
            ).json()
            status = str(resp.get("status", "")).lower()
            if status == "authorized":
                login_ticket = resp.get("loginTicket")
                authorized = True
                break
            if status != "pending":
                raise RuntimeError(f"COROS 授权异常状态：{status}（{resp}）")
            time.sleep(pending["poll_interval"])

        if not authorized:
            return {"authorized": False}
        if not login_ticket:
            raise RuntimeError("COROS 已授权但未返回 loginTicket")

        code = self._resolve_code(pending, login_ticket)
        token = self._exchange_code(pending, code)
        self._save_json(self._token_path(qq), token)
        self._pending_path(qq).unlink(missing_ok=True)
        return {"authorized": True}

    def _register_client(self) -> str:
        r = httpx.post(
            f"{self._issuer}/connect/register",
            json={
                "client_name": self.CLIENT_NAME,
                "redirect_uris": [self.REDIRECT_URI],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": self.SCOPE,
                "token_endpoint_auth_method": "none",
            },
            headers={**self._UA, "Content-Type": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        client_id = r.json().get("client_id")
        if not client_id:
            raise RuntimeError(f"COROS 客户端注册失败：{r.text[:200]}")
        return client_id

    def _resolve_code(self, pending: dict, login_ticket: str) -> str:
        au = pending["authorize_url"] + "&login_ticket=" + login_ticket
        r = httpx.get(au, headers=self._UA, timeout=30, follow_redirects=False)
        loc = r.headers.get("location", "")
        if r.status_code not in (302, 303) or not loc:
            raise RuntimeError(f"COROS authorize 未重定向：{r.status_code} {r.text[:200]}")
        qs = parse_qs(urlparse(loc).query)
        code = qs.get("code", [""])[0]
        state = qs.get("state", [""])[0]
        if not code or state != pending["state"]:
            raise RuntimeError("COROS 授权 code/state 校验失败")
        return code

    def _exchange_code(self, pending: dict, code: str) -> dict:
        r = httpx.post(
            f"{self._issuer}/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "client_id": pending["client_id"],
                "code": code,
                "redirect_uri": self.REDIRECT_URI,
                "code_verifier": pending["verifier"],
            },
            headers=self._UA,
            timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError(f"COROS token 交换失败：{r.status_code} {r.text[:200]}")
        token = r.json()
        token["client_id"] = pending["client_id"]
        token["expires_at_epoch"] = int(time.time()) + int(token.get("expires_in", 3600))
        return token

    @staticmethod
    def _pkce_verifier() -> str:
        return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")

    @staticmethod
    def _pkce_challenge(verifier: str) -> str:
        return (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )

    # ------------------------------------------------------------------
    # token 管理
    # ------------------------------------------------------------------

    def _ensure_token(self, qq: str) -> str:
        token = self._load_json(self._token_path(qq))
        if not token or not token.get("access_token"):
            raise RuntimeError("COROS 尚未授权，请发送「绑定 coros」完成授权")
        if time.time() > token.get("expires_at_epoch", 0) - 300:
            token = self._refresh(qq, token)
        return token["access_token"]

    def _refresh(self, qq: str, token: dict) -> dict:
        if not token.get("refresh_token"):
            raise RuntimeError("COROS token 已失效且无 refresh_token，请重新「绑定 coros」授权")
        r = httpx.post(
            f"{self._issuer}/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": token.get("client_id"),
                "refresh_token": token["refresh_token"],
            },
            headers=self._UA,
            timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError(f"COROS token 刷新失败：{r.status_code} {r.text[:200]}")
        new_token = r.json()
        new_token["client_id"] = token.get("client_id")
        if not new_token.get("refresh_token"):
            new_token["refresh_token"] = token.get("refresh_token")
        new_token["expires_at_epoch"] = int(time.time()) + int(new_token.get("expires_in", 3600))
        self._save_json(self._token_path(qq), new_token)
        return new_token

    # ------------------------------------------------------------------
    # 拉取与解析
    # ------------------------------------------------------------------

    def fetch_daily(self, qq: str, d: date) -> DailyStats:
        delta = (date.today() - d).days
        days = min(30, max(2, delta + 2))
        text = self._query_daily_health(qq, days)
        stats = self._parse_daily(text, d)

        # 运动记录：距离 / 单次最长 / 配速 / 心率 / 活动数
        # 先初始化为空结果，避免接口异常时下方引用未定义的 parsed。
        parsed = self._parse_sport_records("")
        try:
            day_str = d.strftime("%Y%m%d")
            sport = self._query_sport_records(qq, day_str, day_str)
            parsed = self._parse_sport_records(sport)
        except Exception:
            # 距离等为附加数据，接口异常不影响步数等主指标
            pass

        stats.distance_km = parsed["total_distance_km"]
        stats.activities_count = parsed["count"]
        stats.max_activity_distance_km = parsed["max_distance_km"]
        stats.avg_pace_sec_per_km = parsed["avg_pace_sec_per_km"]
        stats.avg_hr = parsed["avg_hr"]

        # 爬升：querySportRecords 不含爬升，需按活动详情下钻累加（封顶避免打爆）
        try:
            ascent = 0.0
            for label_id, sport_type in parsed["activities"][:10]:
                try:
                    detail = self._query_activity_detail(qq, label_id, sport_type)
                    ascent += self._parse_activity_ascent(detail)
                except Exception:
                    continue
            stats.ascent_meters = round(ascent, 2)
        except Exception:
            pass

        # 运动负荷：官方按天聚合的评估值（Short-Term Load）
        try:
            load_text = self._query_training_load(qq, days)
            stats.training_load = self._parse_training_load(load_text, d)
        except Exception:
            pass

        return stats

    def _call_tool(self, qq: str, name: str, arguments: dict, req_id: int = 1) -> str:
        """调用 COROS MCP 工具并返回解码后的 text（stateless JSON-RPC）。"""
        token = self._ensure_token(qq)
        r = httpx.post(
            self._mcp_url,
            json={
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                **self._UA,
            },
            timeout=30,
        )
        r.raise_for_status()
        result = r.json().get("result") or {}
        if result.get("isError"):
            raise RuntimeError(f"COROS MCP 调用失败：{result}")
        for c in result.get("content", []):
            if c.get("type") == "text":
                return self._decode_text(c.get("text", ""))
        return ""

    def _query_daily_health(self, qq: str, days: int) -> str:
        return self._call_tool(qq, "queryDailyHealthData", {"days": days})

    @staticmethod
    def _decode_text(text: str) -> str:
        # MCP 返回的 text 是被 JSON 编码的字符串（带引号），需解一层
        if text.startswith('"'):
            try:
                decoded = json.loads(text)
                if isinstance(decoded, str):
                    return decoded
            except (json.JSONDecodeError, TypeError):
                pass
        return text

    def _query_sport_records(self, qq: str, start_date: str, end_date: str) -> str:
        return self._call_tool(
            qq,
            "querySportRecords",
            {
                "startDate": start_date,
                "endDate": end_date,
                "sportTypeCodes": None,
                "minDistanceKm": None,
                "maxDistanceKm": None,
                "minDurationMinutes": None,
                "maxDurationMinutes": None,
                "maxAveragePace": None,
                "locationKeyword": None,
                "limit": 50,
            },
        )

    def _query_activity_detail(self, qq: str, label_id: str, sport_type: str) -> str:
        return self._call_tool(
            qq, "getActivityDetail", {"labelId": label_id, "sportType": int(sport_type)}
        )

    def _query_training_load(self, qq: str, days: int) -> str:
        return self._call_tool(qq, "queryTrainingLoadAssessment", {"days": days})

    def _parse_daily(self, text: str, d: date) -> DailyStats:
        stats = DailyStats(date=d)
        day_str = d.strftime("%Y%m%d")

        m = re.search(r"Resting HR:\s*(\d+)", text)
        if m:
            stats.resting_hr = int(m.group(1))

        blocks = re.split(r"---\s*(\d{8})\s*---", text)
        for i in range(1, len(blocks), 2):
            if blocks[i] != day_str:
                continue
            body = blocks[i + 1]
            m = re.search(r"Steps:\s*([\d,]+)", body)
            if m:
                stats.steps = int(m.group(1).replace(",", ""))
            m = re.search(r"Calories:\s*([\d,]+)", body)
            if m:
                stats.calories = int(m.group(1).replace(",", ""))
            m = re.search(r"Exercise:\s*(?:(\d+)\s*h\s*)?(\d+)\s*min", body)
            if m:
                stats.active_minutes = int(m.group(1) or 0) * 60 + int(m.group(2))
            m = re.search(r"Total:\s*(\d+)\s*h\s*(\d+)\s*min", body)
            if m:
                stats.sleep_hours = round(int(m.group(1)) + int(m.group(2)) / 60, 2)
            break
        return stats

    @staticmethod
    def _parse_sport_records(text: str) -> dict:
        """从运动记录文本提取聚合指标。

        真实格式（每条记录）：
            Duration: 40:17 | Distance: 8.00 km
            Average Pace: 5:02 /km | Avg HR: 160 bpm | Calories: 598 kcal
            LabelId: 4803... | SportType: 100
        返回 dict：total_distance_km / count / max_distance_km /
        avg_pace_sec_per_km / avg_hr / activities[(label_id, sport_type)]。
        """
        empty = {
            "total_distance_km": 0.0,
            "count": 0,
            "max_distance_km": 0.0,
            "avg_pace_sec_per_km": 0.0,
            "avg_hr": 0,
            "activities": [],
        }
        if not text:
            return empty

        count = 0
        m = re.search(r"\((\d+)\s+records?\)", text)
        if m:
            count = int(m.group(1))

        # 时长 + 距离（同一条记录同一行）
        recs = []
        for m in re.finditer(
            r"Duration:\s*(\d+):(\d{1,2})(?::(\d{1,2}))?\s*\|\s*Distance:\s*([\d.]+)\s*(km|m)\b",
            text,
        ):
            if m.group(3):
                dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            else:
                dur = int(m.group(1)) * 60 + int(m.group(2))
            km = float(m.group(4))
            if m.group(5) == "m":
                km /= 1000.0
            recs.append({"dur": dur, "km": km, "hr": 0, "pace": 0.0})

        # 配速 / 心率与上面同序（每条记录一条），按索引对齐
        paces = re.findall(r"Average Pace:\s*(\d+):(\d{1,2})\s*/km", text)
        hrs = re.findall(r"Avg HR:\s*(\d+)\s*bpm", text)
        for i, rec in enumerate(recs):
            if i < len(paces):
                rec["pace"] = int(paces[i][0]) * 60 + int(paces[i][1])
            if i < len(hrs):
                rec["hr"] = int(hrs[i])

        total_km = round(sum(r["km"] for r in recs), 2)
        total_dur = sum(r["dur"] for r in recs)
        # 平均配速 = 总运动时长 / 总距离（距离加权，最贴近「当天整体配速」）
        avg_pace = round(total_dur / total_km, 1) if total_km > 0 else 0.0
        # 平均心率 = 按时长加权
        weighted_hr = sum(r["hr"] * r["dur"] for r in recs)
        avg_hr = int(round(weighted_hr / total_dur)) if total_dur > 0 else 0

        return {
            "total_distance_km": total_km,
            "count": count,
            "max_distance_km": round(max((r["km"] for r in recs), default=0.0), 2),
            "avg_pace_sec_per_km": avg_pace,
            "avg_hr": avg_hr,
            "activities": re.findall(r"LabelId:\s*(\d+)\s*\|\s*SportType:\s*(\d+)", text),
        }

    @staticmethod
    def _parse_activity_ascent(text: str) -> float:
        """从 getActivityDetail 文本提取累计爬升（米）。

        真实格式：``Elevation Gain / Loss: 0 m / 2 m``（取 Gain 侧）。
        """
        if not text:
            return 0.0
        m = re.search(r"Elevation Gain\s*/\s*Loss:\s*([\d.]+)\s*m", text)
        return float(m.group(1)) if m else 0.0

    @staticmethod
    def _parse_training_load(text: str, d: date) -> float:
        """从 queryTrainingLoadAssessment 文本提取某天的运动负荷（Short-Term Load）。"""
        if not text:
            return 0.0
        day_str = d.strftime("%Y-%m-%d")
        for block in text.split("\n\n"):
            if block.startswith(day_str):
                m = re.search(r"Short-Term Load:\s*([\d.]+)", block)
                if m:
                    return float(m.group(1))
        return 0.0

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    @staticmethod
    def _load_json(path: Path) -> Optional[dict]:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def _save_json(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
