"""QQ音乐点歌（LLM 函数调用工具）。

提供 `play_qq_music` 函数工具，供大模型在主客观对话中直接调用：
大模型判断用户想听歌时，会以歌名为参数调用该工具，插件通过 QQ 音乐
官方接口搜索歌曲、解析音频地址并下载，再以音频文件或语音形式发送。
"""

import asyncio
import base64
import gzip
import json
import re
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import aiofiles

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.message.components import File, Record
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

SEARCH_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0"
)
API_HEADERS = {
    "User-Agent": USER_AGENT,
    "Content-Type": "application/json",
    "Referer": "https://y.qq.com/",
    "Accept-Encoding": "gzip",
}
DOWNLOAD_HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": "https://y.qq.com/",
}


class QQMusicClient:
    """QQ音乐搜索与音频地址解析。"""

    def __init__(self, uin: str, cookie: str = "", proxy: str = ""):
        self.uin = uin or "10001"
        self.cookie = cookie or ""
        self.proxy = proxy or ""

    @staticmethod
    def _hash33(value: str, seed: int = 5381) -> int:
        for char in value:
            seed += (seed << 5) + ord(char)
        return seed & 0x7FFFFFFF

    @classmethod
    def _get_gtk(cls, cookie: str) -> int:
        values: dict[str, str] = {}
        for item in cookie.split(";"):
            key, separator, value = item.strip().partition("=")
            if separator:
                values[key] = value
        return cls._hash33(
            values.get("qqmusic_key")
            or values.get("qm_keyst")
            or values.get("p_skey")
            or ""
        )

    def _opener(self) -> urllib.request.OpenerDirector:
        proxy_handler = {"http": self.proxy, "https": self.proxy} if self.proxy else {}
        return urllib.request.build_opener(urllib.request.ProxyHandler(proxy_handler))

    async def _post_musicu(self, body: dict[str, Any]) -> dict[str, Any] | None:
        headers = dict(API_HEADERS)
        if self.cookie:
            headers["Cookie"] = self.cookie
        request = urllib.request.Request(
            SEARCH_URL,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        opener = self._opener()
        try:
            response = await asyncio.to_thread(opener.open, request, timeout=15)
            try:
                raw = await asyncio.to_thread(response.read)
                encoding = response.headers.get("Content-Encoding")
            finally:
                response.close()
        except Exception as exc:
            logger.warning(f"QQ音乐接口请求失败: {exc}")
            return None

        if encoding == "gzip":
            raw = gzip.decompress(raw)
        try:
            data = json.loads(raw.decode("utf-8", "ignore"))
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    async def search(self, keyword: str, limit: int = 8) -> list[dict[str, Any]]:
        """搜索歌曲并返回基础信息列表。"""
        body = {
            "comm": {
                "ct": "19",
                "cv": "1859",
                "uin": self.uin,
                "g_tk": self._get_gtk(self.cookie),
            },
            "req": {
                "module": "music.search.SearchCgiService",
                "method": "DoSearchForQQMusicDesktop",
                "param": {
                    "num_per_page": limit,
                    "page_num": 1,
                    "query": keyword,
                    "search_type": 0,
                },
            },
        }

        data = None
        for attempt in range(4):
            data = await self._post_musicu(body)
            if data and self._song_items(data):
                break
            if attempt < 3:
                await asyncio.sleep(0.8 + 0.6 * attempt)

        songs: list[dict[str, Any]] = []
        for item in self._song_items(data):
            singers = "/".join(
                singer.get("name")
                for singer in item.get("singer", [])
                if isinstance(singer, dict) and singer.get("name")
            )
            album = item.get("album") if isinstance(item.get("album"), dict) else {}
            album_mid = album.get("mid") or ""
            songs.append(
                {
                    "id": str(item.get("id") or ""),
                    "mid": item.get("mid") or "",
                    "name": item.get("name") or "",
                    "artists": singers,
                    "cover_url": (
                        f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{album_mid}.jpg"
                        if album_mid
                        else None
                    ),
                }
            )
        return songs

    @staticmethod
    def _song_items(data: dict[str, Any] | None) -> list[dict[str, Any]]:
        value: Any = data
        for key in ("req", "data", "body", "song", "list"):
            if not isinstance(value, dict):
                return []
            value = value.get(key)
        return value if isinstance(value, list) else []

    async def resolve_audio_url(self, mid: str) -> str | None:
        """通过 GetVkey 解析歌曲 M500 音频地址。"""
        try:
            comm_uin = int(self.uin)
        except ValueError:
            logger.warning("QQ音乐 UIN 必须为数字，无法解析音频地址")
            return None
        body = {
            "comm": {"uin": comm_uin, "format": "json", "ct": 24, "cv": 0},
            "req_0": {
                "module": "vkey.GetVkeyServer",
                "method": "CgiGetVkey",
                "param": {
                    "filename": [f"M500{mid}{mid}.mp3"],
                    "guid": "10000",
                    "songmid": [mid],
                    "songtype": [0],
                    "uin": self.uin,
                    "loginflag": 1,
                    "platform": "20",
                },
            },
        }
        data = await self._post_musicu(body)
        request_data = data.get("req_0") if data else None
        response_data = (
            request_data.get("data") if isinstance(request_data, dict) else None
        )
        if not isinstance(response_data, dict):
            response_data = {}
        servers = response_data.get("sip") or []
        url_info = response_data.get("midurlinfo") or []
        if (
            servers
            and isinstance(url_info, list)
            and url_info
            and isinstance(url_info[0], dict)
            and url_info[0].get("purl")
        ):
            return servers[0] + url_info[0]["purl"]
        return None

    async def download(self, url: str, target_dir: Path) -> Path | None:
        """下载音频到临时目录。"""
        target_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_dir / f"{uuid.uuid4().hex}.mp3"
        request = urllib.request.Request(url, headers=DOWNLOAD_HEADERS)
        opener = self._opener()
        try:
            response = await asyncio.to_thread(opener.open, request, timeout=60)
            try:
                if response.status != 200:
                    return None
                async with aiofiles.open(file_path, "wb") as file:
                    while chunk := await asyncio.to_thread(response.read, 64 * 1024):
                        await file.write(chunk)
            finally:
                response.close()
            if file_path.stat().st_size == 0:
                file_path.unlink(missing_ok=True)
                return None
            return file_path
        except Exception as exc:
            file_path.unlink(missing_ok=True)
            logger.error(f"QQ音乐音频下载失败: {exc}")
            return None


class QQMusicVoicePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.uin = str(config.get("uin", "10001") or "10001")
        self.cookie = config.get("cookie", "") or ""
        self.proxy = config.get("proxy", "") or ""
        self.send_mode = config.get("send_mode", "voice_first") or "voice_first"
        self.temp_dir = Path(get_astrbot_temp_path()) / "qqmusic_voice"
        self.client = QQMusicClient(self.uin, self.cookie, self.proxy)

    async def terminate(self):
        """插件卸载时无持久化连接，无需额外清理。"""

    @staticmethod
    def _pick_song(
        songs: list[dict[str, Any]], keyword: str, artist: str
    ) -> dict[str, Any] | None:
        keyword_lower = keyword.lower()
        artist_lower = artist.lower()
        best: dict[str, Any] | None = None
        best_score = -1
        for song in songs:
            name = (song.get("name") or "").lower()
            artists = (song.get("artists") or "").lower()
            score = 0
            if name == keyword_lower:
                score += 5
            elif keyword_lower in name or name in keyword_lower:
                score += 3
            if artist_lower and artist_lower in artists:
                score += 3
            elif keyword_lower in artists or artists in keyword_lower:
                score += 1
            if score > best_score:
                best_score = score
                best = song
        return best

    @staticmethod
    def _safe_filename(name: str) -> str:
        safe = re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "歌曲"
        return f"{safe}.mp3"

    async def _send_onebot_record(self, event: AiocqhttpMessageEvent, file_ref: str) -> bool:
        """通过 OneBot API 发送 record 段（file_ref 为 URL 或 base64://）。"""
        payload = {"message": [{"type": "record", "data": {"file": file_ref}}]}
        if event.is_private_chat():
            payload["user_id"] = event.get_sender_id()
            result = await event.bot.api.call_action("send_private_msg", **payload)
        else:
            payload["group_id"] = event.get_group_id()
            result = await event.bot.api.call_action("send_group_msg", **payload)
        return result is not None

    async def _send_record(self, event: AstrMessageEvent, audio_url: str) -> bool:
        """发送 QQ 语音：URL 优先，失败则下载后 base64（跨容器可用）。"""
        # aiocqhttp：本地文件 URI 需要 AstrBot 与 NapCat 共享目录，跨容器会
        # 因 NapCat 找不到文件而失败，因此改为 URL / base64 两种自包含方式。
        if isinstance(event, AiocqhttpMessageEvent):
            try:
                if await self._send_onebot_record(event, audio_url):
                    return True
            except Exception as exc:
                logger.warning(f"URL 语音发送失败，尝试 base64: {exc}")

            file_path = await self.client.download(audio_url, self.temp_dir)
            if not file_path:
                return False
            try:
                data = await asyncio.to_thread(file_path.read_bytes)
                encoded = base64.b64encode(data).decode("ascii")
                return await self._send_onebot_record(event, f"base64://{encoded}")
            except Exception as exc:
                logger.warning(f"base64 语音发送失败: {exc}")
                return False
            finally:
                self._schedule_cleanup(file_path)

        try:
            await event.send(event.chain_result([Record.fromURL(audio_url)]))
            return True
        except Exception as exc:
            logger.warning(f"语音消息发送失败: {exc}")
            return False

    async def _send_file(self, event: AstrMessageEvent, song: dict, audio_url: str) -> bool:
        """发送完整音频文件（用 URL，跨容器可用、可播放/保存）。"""
        file_name = self._safe_filename(
            f"{song.get('name') or '歌曲'} - {song.get('artists') or '未知歌手'}"
        )
        try:
            await event.send(
                event.chain_result([File(name=file_name, url=audio_url)])
            )
            return True
        except Exception as exc:
            logger.warning(f"音频文件发送失败: {exc}")
            return False

    async def _send_audio(
        self, event: AstrMessageEvent, song: dict, audio_url: str
    ) -> tuple[bool, str]:
        """按配置发送音频，返回 (是否成功, 发送方式)。"""
        mode = self.send_mode
        voice_ok = False
        if mode in ("voice_first", "voice", "both"):
            voice_ok = await self._send_record(event, audio_url)
            if voice_ok and mode != "both":
                return True, "voice"
            if not voice_ok and mode == "voice":
                return False, "voice"

        file_ok = await self._send_file(event, song, audio_url)
        if file_ok:
            if mode == "both" and voice_ok:
                return True, "both"
            return True, "file"
        if voice_ok:
            return True, "voice"
        return False, mode

    def _schedule_cleanup(self, path: Path) -> None:
        asyncio.create_task(self._cleanup(path))

    @staticmethod
    async def _cleanup(path: Path) -> None:
        await asyncio.sleep(120)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    @filter.llm_tool()
    async def play_qq_music(
        self,
        event: AstrMessageEvent,
        song_name: str,
        artist: str = "",
    ):
        """当用户想听歌、点歌或播放音乐时，搜索并播放 QQ 音乐歌曲。

        Args:
            song_name(string): 歌曲名称或搜索关键词，例如“晴天”或“晴天 周杰伦”。
            artist(string): 可选歌手名；填写后优先匹配该歌手的歌曲。
        """
        keyword = (song_name or "").strip()
        if not keyword:
            return "请提供要播放的歌曲名称"

        songs = await self.client.search(keyword, limit=8)
        if not songs:
            return f"未在 QQ 音乐找到与「{keyword}」相关的歌曲"

        song = self._pick_song(songs, keyword, (artist or "").strip())
        if not song:
            return f"未找到「{keyword}」的可靠匹配歌曲"

        audio_url = await self.client.resolve_audio_url(song.get("mid") or "")
        if not audio_url:
            return f"《{song.get('name')}》音频获取失败（可能为 VIP 或无版权歌曲）"

        sent, mode = await self._send_audio(event, song, audio_url)
        title = f"{song.get('name')} - {song.get('artists') or '未知歌手'}"
        if not sent:
            return f"《{title}》发送失败，播放链接：{audio_url}"
        if mode == "voice":
            return f"已通过 QQ 音乐发送《{title}》语音"
        if mode == "both":
            return f"已通过 QQ 音乐发送《{title}》语音和音频文件"
        return f"已通过 QQ 音乐发送《{title}》音频文件"
