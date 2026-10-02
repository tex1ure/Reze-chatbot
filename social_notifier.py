"""
Social Media Notifier for Discord Webhooks
Monitors YouTube channel and Instagram account for new posts and posts direct links to Discord.
"""

import os
import sys
import json
import time
import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional, Dict, Any

import aiohttp
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [Notifier] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("SocialNotifier")

# Configuration from environment
WEBHOOK_URL = os.getenv("NOTIFIER_DISCORD_WEBHOOK_URL", "").strip()
YT_CHANNEL_ID = os.getenv("NOTIFIER_YT_CHANNEL_ID", "").strip()
YT_HANDLE = os.getenv("NOTIFIER_YT_HANDLE", "").strip()
IG_USERNAME = os.getenv("NOTIFIER_IG_USERNAME", "").strip()
IG_SESSIONID = os.getenv("NOTIFIER_IG_SESSIONID", "").strip()
POLL_INTERVAL = int(os.getenv("NOTIFIER_POLL_INTERVAL", "180"))
PING_ROLE_ID = os.getenv("NOTIFIER_PING_ROLE_ID", "").strip()
USE_DDINSTAGRAM = os.getenv("NOTIFIER_IG_USE_DDINSTAGRAM", "true").lower() in ("true", "1", "yes")

# State file location
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notifier_state.json")

HEADERS_DEFAULT = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

class StateManager:
    """Manages persistent state of last seen post IDs."""
    def __init__(self, filepath: str = STATE_FILE):
        self.filepath = filepath
        self.state: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not load state file ({e}), initializing fresh state.")
        return {"last_yt_id": None, "last_ig_id": None, "updated_at": None}

    def save(self):
        try:
            self.state["updated_at"] = datetime.utcnow().isoformat()
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save state to {self.filepath}: {e}")

    @property
    def last_yt_id(self) -> Optional[str]:
        return self.state.get("last_yt_id")

    @last_yt_id.setter
    def last_yt_id(self, val: str):
        self.state["last_yt_id"] = val
        self.save()

    @property
    def last_ig_id(self) -> Optional[str]:
        return self.state.get("last_ig_id")

    @last_ig_id.setter
    def last_ig_id(self, val: str):
        self.state["last_ig_id"] = val
        self.save()


class YouTubeMonitor:
    """Fetches new uploads from a YouTube channel via official Atom/XML RSS feeds."""
    def __init__(self, channel_id: str = "", handle: str = ""):
        self.channel_id = channel_id
        self.handle = handle

    async def resolve_channel_id(self, session: aiohttp.ClientSession) -> Optional[str]:
        """Resolves a channel handle (@username) to a YouTube channel ID (UC...)."""
        if self.channel_id:
            return self.channel_id
        if not self.handle:
            return None

        handle_clean = self.handle.lstrip("@")
        url = f"https://www.youtube.com/@{handle_clean}"
        logger.info(f"Resolving YouTube handle @{handle_clean} to Channel ID...")

        try:
            async with session.get(url, headers=HEADERS_DEFAULT, timeout=15) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    # Try meta tag
                    match = re.search(r'<meta itemprop="identifier" content="([a-zA-Z0-9_-]{24})"', text)
                    if match:
                        self.channel_id = match.group(1)
                        logger.info(f"Resolved @{handle_clean} to Channel ID: {self.channel_id}")
                        return self.channel_id

                    # Try channelId in JSON
                    match = re.search(r'"channelId":"([a-zA-Z0-9_-]{24})"', text)
                    if match:
                        self.channel_id = match.group(1)
                        logger.info(f"Resolved @{handle_clean} to Channel ID: {self.channel_id}")
                        return self.channel_id
                else:
                    logger.warning(f"YouTube handle page returned HTTP {resp.status}")
        except Exception as e:
            logger.error(f"Failed to resolve YouTube handle: {e}")
        return None

    async def get_latest_video(self, session: aiohttp.ClientSession) -> Optional[Dict[str, Any]]:
        """Fetches the latest video from the YouTube RSS XML feed."""
        cid = await self.resolve_channel_id(session)
        if not cid:
            return None

        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
        try:
            async with session.get(feed_url, headers=HEADERS_DEFAULT, timeout=15) as resp:
                if resp.status != 200:
                    logger.warning(f"YouTube RSS returned HTTP {resp.status} for {feed_url}")
                    return None
                xml_data = await resp.text()

            # Parse Atom XML
            root = ET.fromstring(xml_data)
            # Atom namespace mapping
            ns = {
                "atom": "http://www.w3.org/2005/Atom",
                "yt": "http://www.youtube.com/xml/schemas/2015",
                "media": "http://search.yahoo.com/mrss/"
            }

            entry = root.find("atom:entry", ns)
            if entry is None:
                return None

            video_id_elem = entry.find("yt:videoId", ns)
            title_elem = entry.find("atom:title", ns)
            link_elem = entry.find("atom:link", ns)
            published_elem = entry.find("atom:published", ns)
            author_elem = entry.find("atom:author/atom:name", ns)

            if video_id_elem is None or not video_id_elem.text:
                return None

            video_id = video_id_elem.text
            title = title_elem.text if title_elem is not None and title_elem.text else "New Video"
            url = link_elem.attrib.get("href", f"https://www.youtube.com/watch?v={video_id}") if link_elem is not None else f"https://www.youtube.com/watch?v={video_id}"
            author = author_elem.text if author_elem is not None and author_elem.text else "YouTube"

            return {
                "id": video_id,
                "title": title,
                "url": url,
                "author": author,
                "published": published_elem.text if published_elem is not None else None
            }
        except Exception as e:
            logger.error(f"Error fetching YouTube feed: {e}")
            return None


class InstagramMonitor:
    """Monitors public Instagram posts using public web endpoints."""
    def __init__(self, username: str = ""):
        self.username = username.lstrip("@").strip()

    async def get_latest_post(self, session: aiohttp.ClientSession) -> Optional[Dict[str, Any]]:
        if not self.username:
            return None

        # Method 1: Web profile info endpoint with standard Instagram Web App ID
        url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={self.username}"
        headers = {
            **HEADERS_DEFAULT,
            "x-ig-app-id": "936619743392459",
            "Referer": f"https://www.instagram.com/{self.username}/",
            "Accept": "*/*"
        }
        if IG_SESSIONID:
            headers["Cookie"] = f"sessionid={IG_SESSIONID};"

        try:
            async with session.get(url, headers=headers, timeout=15) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    user = data.get("data", {}).get("user", {})
                    timeline = user.get("edge_owner_to_timeline_media", {})
                    edges = timeline.get("edges", [])
                    if edges:
                        node = edges[0].get("node", {})
                        shortcode = node.get("shortcode")
                        if shortcode:
                            caption = ""
                            caption_edges = node.get("edge_media_to_caption", {}).get("edges", [])
                            if caption_edges:
                                caption = caption_edges[0].get("node", {}).get("text", "")

                            raw_url = f"https://www.instagram.com/p/{shortcode}/"
                            embed_url = f"https://ddinstagram.com/p/{shortcode}/" if USE_DDINSTAGRAM else raw_url

                            return {
                                "id": shortcode,
                                "url": raw_url,
                                "embed_url": embed_url,
                                "caption": caption[:150] + ("..." if len(caption) > 150 else ""),
                                "author": self.username,
                                "timestamp": node.get("taken_at_timestamp")
                            }
                elif resp.status in (401, 403, 429):
                    logger.debug(f"Instagram profile endpoint returned HTTP {resp.status}. Trying public HTML parser fallback.")
        except Exception as e:
            logger.debug(f"Instagram JSON API error: {e}. Trying public HTML fallback.")

        # Method 2: Public HTML Fallback
        try:
            profile_url = f"https://www.instagram.com/{self.username}/"
            async with session.get(profile_url, headers=HEADERS_DEFAULT, timeout=15) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    # Look for shortcode in post links /p/SHORTCODE/
                    matches = re.findall(r'/p/([a-zA-Z0-9_-]{10,12})/', html)
                    if matches:
                        shortcode = matches[0]
                        raw_url = f"https://www.instagram.com/p/{shortcode}/"
                        embed_url = f"https://ddinstagram.com/p/{shortcode}/" if USE_DDINSTAGRAM else raw_url
                        return {
                            "id": shortcode,
                            "url": raw_url,
                            "embed_url": embed_url,
                            "caption": f"Latest post from @{self.username}",
                            "author": self.username
                        }
        except Exception as e:
            logger.debug(f"Instagram HTML fallback error: {e}")

        return None


class DiscordWebhookDispatcher:
    """Dispatches direct link notifications to a Discord Webhook."""
    def __init__(self, webhook_url: str, role_id: str = ""):
        self.webhook_url = webhook_url
        self.role_id = role_id

    def _format_mention(self) -> str:
        if not self.role_id:
            return ""
        if self.role_id.lower() in ("everyone", "@everyone"):
            return "@everyone "
        if self.role_id.lower() in ("here", "@here"):
            return "@here "
        return f"<@&{self.role_id}> "

    async def send_youtube_notification(self, session: aiohttp.ClientSession, video: Dict[str, Any]) -> bool:
        mention = self._format_mention()
        # Direct link posting for native Discord playable embed
        content = (
            f"{mention}🔴 **New YouTube Video Dropped!**\n"
            f"**{video['title']}**\n"
            f"{video['url']}"
        )
        payload = {
            "username": f"{video.get('author', 'YouTube')} Notifier",
            "avatar_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/0/09/YouTube_full-color_icon_%282017%29.svg/512px-YouTube_full-color_icon_%282017%29.svg.png",
            "content": content
        }
        return await self._dispatch(session, payload)

    async def send_instagram_notification(self, session: aiohttp.ClientSession, post: Dict[str, Any]) -> bool:
        mention = self._format_mention()
        target_link = post.get("embed_url", post["url"])
        caption_snippet = f"\n> {post['caption']}" if post.get("caption") else ""
        
        # Direct link posting so Discord automatically expands the reel/post
        content = (
            f"{mention}📸 **New Instagram Post from @{post['author']}!**"
            f"{caption_snippet}\n"
            f"{target_link}"
        )
        payload = {
            "username": "Instagram Notifier",
            "avatar_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a5/Instagram_icon.png/512px-Instagram_icon.png",
            "content": content
        }
        return await self._dispatch(session, payload)

    async def _dispatch(self, session: aiohttp.ClientSession, payload: Dict[str, Any]) -> bool:
        if not self.webhook_url:
            logger.warning("No Discord Webhook URL configured. Skipping message dispatch.")
            return False

        try:
            async with session.post(self.webhook_url, json=payload, timeout=10) as resp:
                if resp.status in (200, 204):
                    logger.info("Successfully posted notification to Discord webhook!")
                    return True
                else:
                    body = await resp.text()
                    logger.error(f"Failed to post to webhook (HTTP {resp.status}): {body}")
                    return False
        except Exception as e:
            logger.error(f"Error dispatching webhook: {e}")
            return False


async def run_notifier(test_mode: bool = False):
    """Main polling loop."""
    logger.info("Starting Social Media Notifier daemon...")
    logger.info(f"Poll Interval: {POLL_INTERVAL} seconds")
    logger.info(f"YouTube Target: Channel ID '{YT_CHANNEL_ID}' | Handle '{YT_HANDLE}'")
    logger.info(f"Instagram Target: '@{IG_USERNAME}'")
    logger.info(f"Webhook Configured: {'Yes' if WEBHOOK_URL else 'No (Please set NOTIFIER_DISCORD_WEBHOOK_URL)'}")

    state = StateManager()
    yt_monitor = YouTubeMonitor(channel_id=YT_CHANNEL_ID, handle=YT_HANDLE)
    ig_monitor = InstagramMonitor(username=IG_USERNAME)
    dispatcher = DiscordWebhookDispatcher(webhook_url=WEBHOOK_URL, role_id=PING_ROLE_ID)

    async with aiohttp.ClientSession() as session:
        if test_mode:
            logger.info("--- Running in TEST mode: Sending immediate test notification to webhook ---")
            if YT_CHANNEL_ID or YT_HANDLE:
                latest_yt = await yt_monitor.get_latest_video(session)
                if latest_yt:
                    logger.info(f"Dispatching test YouTube post: {latest_yt['title']}")
                    await dispatcher.send_youtube_notification(session, latest_yt)
            if IG_USERNAME:
                latest_ig = await ig_monitor.get_latest_post(session)
                if latest_ig:
                    logger.info(f"Dispatching test Instagram post: {latest_ig['id']}")
                    await dispatcher.send_instagram_notification(session, latest_ig)
            logger.info("Test dispatch finished.")
            return

        # Initial baseline check if no state exists
        if not state.last_yt_id and (YT_CHANNEL_ID or YT_HANDLE):
            logger.info("Initializing baseline state for YouTube...")
            latest_yt = await yt_monitor.get_latest_video(session)
            if latest_yt:
                state.last_yt_id = latest_yt["id"]
                logger.info(f"YouTube baseline set to video ID: {latest_yt['id']} ('{latest_yt['title']}')")

        if not state.last_ig_id and IG_USERNAME:
            logger.info("Initializing baseline state for Instagram...")
            latest_ig = await ig_monitor.get_latest_post(session)
            if latest_ig:
                state.last_ig_id = latest_ig["id"]
                logger.info(f"Instagram baseline set to post ID: {latest_ig['id']}")

        while True:
            try:
                # 1. Check YouTube
                if YT_CHANNEL_ID or YT_HANDLE:
                    latest_yt = await yt_monitor.get_latest_video(session)
                    if latest_yt:
                        if state.last_yt_id is None:
                            state.last_yt_id = latest_yt["id"]
                        elif latest_yt["id"] != state.last_yt_id:
                            logger.info(f"New YouTube video detected: {latest_yt['title']} ({latest_yt['id']})")
                            success = await dispatcher.send_youtube_notification(session, latest_yt)
                            if success:
                                state.last_yt_id = latest_yt["id"]

                # 2. Check Instagram
                if IG_USERNAME:
                    latest_ig = await ig_monitor.get_latest_post(session)
                    if latest_ig:
                        if state.last_ig_id is None:
                            state.last_ig_id = latest_ig["id"]
                        elif latest_ig["id"] != state.last_ig_id:
                            logger.info(f"New Instagram post detected: {latest_ig['id']}")
                            success = await dispatcher.send_instagram_notification(session, latest_ig)
                            if success:
                                state.last_ig_id = latest_ig["id"]

            except Exception as e:
                logger.error(f"Error in polling cycle: {e}", exc_info=True)

            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    is_test = "--test" in sys.argv or "-t" in sys.argv
    try:
        asyncio.run(run_notifier(test_mode=is_test))
    except KeyboardInterrupt:
        logger.info("Notifier stopped by user.")
